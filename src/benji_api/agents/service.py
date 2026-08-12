import hashlib
import logging
import re
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from benji_api.agents.channel_delivery import (
    deliver_linq_replies,
    load_recent_messages,
    mark_run_failed,
    set_typing,
)
from benji_api.agents.followups import schedule_follow_up
from benji_api.agents.locking import conversation_turn_lock
from benji_api.agents.prompts import PromptModule, build_benji_instructions
from benji_api.agents.prompts.base import DOT_PROMPT_VERSION
from benji_api.agents.prompts.group import build_group_module
from benji_api.agents.prompts.memory import build_memory_module
from benji_api.agents.prompts.relationship import build_relationship_module
from benji_api.agents.prompts.wake import build_follow_up_module, build_user_event_module
from benji_api.agents.relationship import RelationshipState, load_relationship_state
from benji_api.agents.results import PersistedReply, PersistedTurn
from benji_api.agents.runner import AgentRunner
from benji_api.agents.text_style import prepare_app_completion_bubbles, prepare_text_bubbles
from benji_api.agents.tools import ToolRegistry
from benji_api.agents.types import AgentMessage, ModelProvider, ToolContext
from benji_api.config import Settings
from benji_api.db.session import async_session_factory
from benji_api.integrations.linq.client import LinqClient
from benji_api.memory.service import enqueue_memory_job, retrieve_memory_context
from benji_api.memory.types import EmbeddingProvider, MemoryContext
from benji_api.models.agent import (
    AgentFollowUp,
    AgentFollowUpStatus,
    AgentRun,
    AgentRunPurpose,
    AgentRunStatus,
    AgentToolCall,
    ToolCallStatus,
)
from benji_api.models.channel import (
    Conversation,
    ConversationKind,
    Message,
    MessageDirection,
    MessageStatus,
)
from benji_api.models.user import OnboardingStatus, User
from benji_api.services.groups import (
    group_owner_context,
    list_conversation_members,
    member_label,
)
from benji_api.services.language_preferences import apply_language_preference

logger = logging.getLogger(__name__)

_PROMPT_MODULE_NAME_PATTERN = re.compile(r'<prompt_module name="([^"]+)">')

GROUP_SAFE_TOOL_NAMES = {
    "get_current_datetime",
    "search_web",
    "create_personal_app",
}


class AgentWakeCancelled(RuntimeError):
    pass


async def run_agent_turn(
    *,
    conversation_id: UUID,
    user_id: UUID,
    trigger_message_id: UUID,
    source_channel: str,
    source_binding_id: UUID,
    idempotency_key: str,
    provider: ModelProvider,
    tools: ToolRegistry,
    settings: Settings,
    embedding_provider: EmbeddingProvider | None = None,
    state_modules: tuple[PromptModule, ...] = (),
    allow_follow_up: bool = True,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> PersistedTurn:
    factory = session_factory or async_session_factory
    async with conversation_turn_lock(factory, conversation_id=conversation_id):
        return await run_agent_turn_unlocked(
            conversation_id=conversation_id,
            user_id=user_id,
            trigger_message_id=trigger_message_id,
            source_channel=source_channel,
            source_binding_id=source_binding_id,
            idempotency_key=idempotency_key,
            provider=provider,
            tools=tools,
            settings=settings,
            embedding_provider=embedding_provider,
            state_modules=state_modules,
            allow_follow_up=allow_follow_up,
            session_factory=factory,
        )


async def run_agent_turn_unlocked(
    *,
    conversation_id: UUID,
    user_id: UUID,
    trigger_message_id: UUID,
    source_channel: str,
    source_binding_id: UUID,
    idempotency_key: str,
    provider: ModelProvider,
    tools: ToolRegistry,
    settings: Settings,
    embedding_provider: EmbeddingProvider | None = None,
    state_modules: tuple[PromptModule, ...] = (),
    allow_follow_up: bool = True,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> PersistedTurn:
    """Run a turn when the caller already holds the conversation lock."""
    factory = session_factory or async_session_factory
    return await _run_agent_wake(
        conversation_id=conversation_id,
        user_id=user_id,
        trigger_message_id=trigger_message_id,
        trigger_event_id=None,
        purpose=AgentRunPurpose.CONVERSATION,
        wake_type="user_message",
        source_channel=source_channel,
        source_binding_id=source_binding_id,
        idempotency_key=idempotency_key,
        provider=provider,
        tools=tools,
        settings=settings,
        embedding_provider=embedding_provider,
        query=None,
        state_modules=state_modules,
        delivery_provider="linq" if source_channel == "linq" else None,
        allow_follow_up=allow_follow_up,
        chain_depth=0,
        session_factory=factory,
        guard_follow_up_id=None,
    )


async def run_agent_event(
    *,
    conversation_id: UUID,
    user_id: UUID,
    event_id: UUID,
    event_type: str,
    payload: dict[str, object],
    source_binding_id: UUID | None,
    idempotency_key: str,
    delivery_provider: str | None,
    provider: ModelProvider,
    tools: ToolRegistry,
    settings: Settings,
    embedding_provider: EmbeddingProvider | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> PersistedTurn:
    factory = session_factory or async_session_factory
    required_app_url = _required_app_completion_url(event_type=event_type, payload=payload)
    message_only = _is_message_only_app_reminder(event_type=event_type, payload=payload)
    async with conversation_turn_lock(factory, conversation_id=conversation_id):
        return await _run_agent_wake(
            conversation_id=conversation_id,
            user_id=user_id,
            trigger_message_id=None,
            trigger_event_id=event_id,
            purpose=AgentRunPurpose.EVENT,
            wake_type=event_type,
            source_channel="event",
            source_binding_id=source_binding_id,
            idempotency_key=idempotency_key,
            provider=provider,
            tools=ToolRegistry([]) if message_only else tools,
            settings=settings,
            embedding_provider=embedding_provider,
            query=f"{event_type}: {payload}",
            state_modules=(build_user_event_module(event_type=event_type, payload=payload),),
            delivery_provider=delivery_provider,
            allow_follow_up=not message_only,
            chain_depth=0,
            session_factory=factory,
            guard_follow_up_id=None,
            required_app_url=required_app_url,
        )


async def run_follow_up_turn(
    *,
    conversation_id: UUID,
    user_id: UUID,
    follow_up_id: UUID,
    goal: str,
    source_binding_id: UUID | None,
    delivery_provider: str | None,
    provider: ModelProvider,
    tools: ToolRegistry,
    settings: Settings,
    embedding_provider: EmbeddingProvider | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> PersistedTurn:
    factory = session_factory or async_session_factory
    async with conversation_turn_lock(factory, conversation_id=conversation_id):
        return await _run_agent_wake(
            conversation_id=conversation_id,
            user_id=user_id,
            trigger_message_id=None,
            trigger_event_id=None,
            purpose=AgentRunPurpose.FOLLOW_UP,
            wake_type="scheduled_follow_up",
            source_channel="follow_up",
            source_binding_id=source_binding_id,
            idempotency_key=f"benji:follow-up:{follow_up_id}",
            provider=provider,
            tools=tools,
            settings=settings,
            embedding_provider=embedding_provider,
            query=goal,
            state_modules=(build_follow_up_module(goal=goal),),
            delivery_provider=delivery_provider,
            allow_follow_up=False,
            chain_depth=settings.agent_follow_up_max_chain_depth,
            session_factory=factory,
            guard_follow_up_id=follow_up_id,
        )


async def _run_agent_wake(
    *,
    conversation_id: UUID,
    user_id: UUID,
    trigger_message_id: UUID | None,
    trigger_event_id: UUID | None,
    purpose: AgentRunPurpose,
    wake_type: str,
    source_channel: str,
    source_binding_id: UUID | None,
    idempotency_key: str,
    provider: ModelProvider,
    tools: ToolRegistry,
    settings: Settings,
    embedding_provider: EmbeddingProvider | None,
    query: str | None,
    state_modules: tuple[PromptModule, ...],
    delivery_provider: str | None,
    allow_follow_up: bool,
    chain_depth: int,
    session_factory: async_sessionmaker[AsyncSession] | None,
    guard_follow_up_id: UUID | None,
    required_app_url: str | None = None,
) -> PersistedTurn:
    factory = session_factory or async_session_factory
    run_id: UUID | None = None
    existing = await _load_existing_turn(factory, source_channel, idempotency_key)
    if existing is not None:
        return existing

    try:
        async with factory() as session:
            conversation = await session.get(Conversation, conversation_id)
            user = await session.get(User, user_id)
            if conversation is None or user is None:
                raise RuntimeError("Agent conversation or user no longer exists")
            if guard_follow_up_id is not None:
                follow_up = await session.get(AgentFollowUp, guard_follow_up_id)
                if follow_up is None or follow_up.status == AgentFollowUpStatus.CANCELLED.value:
                    raise AgentWakeCancelled("Follow-up was cancelled by a user message")

            messages = await load_recent_messages(
                session,
                conversation_id=conversation_id,
                limit=settings.agent_context_message_limit,
            )
            context_through = await session.scalar(
                select(Message)
                .where(
                    Message.conversation_id == conversation_id,
                    Message.direction == MessageDirection.INBOUND.value,
                )
                .order_by(Message.created_at.desc(), Message.id.desc())
                .limit(1)
            )
            covered_inbound_ids = tuple(
                str(message_id)
                for message_id in (
                    await session.scalars(
                        select(Message.id)
                        .where(
                            Message.conversation_id == conversation_id,
                            Message.direction == MessageDirection.INBOUND.value,
                        )
                        .order_by(Message.created_at.desc(), Message.id.desc())
                        .limit(settings.agent_context_message_limit)
                    )
                ).all()
            )
            if not messages and query:
                messages.append(
                    AgentMessage(
                        role="user",
                        content=f"[trusted system event context: {query}]",
                    )
                )
            trigger = (
                await session.get(Message, trigger_message_id)
                if trigger_message_id is not None
                else None
            )
            memory = MemoryContext()
            relationship = RelationshipState()
            memory_query = query or (trigger.content if trigger is not None else "")
            personal_context_allowed = conversation.kind == ConversationKind.DIRECT.value
            if settings.memory_enabled and personal_context_allowed and memory_query:
                try:
                    memory = await retrieve_memory_context(
                        session,
                        user_id=user_id,
                        query=memory_query,
                        embedding_provider=embedding_provider,
                        limit=settings.memory_context_limit,
                        candidate_limit=settings.memory_candidate_limit,
                    )
                except Exception:
                    logger.warning(
                        "Memory retrieval failed for user %s; continuing without it",
                        user_id,
                        exc_info=True,
                    )
            if personal_context_allowed and trigger is not None:
                try:
                    relationship = await load_relationship_state(
                        session,
                        user_id=user_id,
                        conversation_id=conversation_id,
                        trigger=trigger,
                    )
                except Exception:
                    logger.warning(
                        "Relationship-state retrieval failed for user %s; continuing without it",
                        user_id,
                        exc_info=True,
                    )
            modules = list(state_modules)
            if conversation.kind == ConversationKind.GROUP.value and not any(
                module.name == "group_conversation" for module in modules
            ):
                members = await list_conversation_members(session, conversation_id=conversation.id)
                owner_name, owner_basis = group_owner_context(
                    members, source=conversation.group_owner_source
                )
                modules.insert(
                    0,
                    build_group_module(
                        title=conversation.title or "group with dot",
                        current_speaker="the group",
                        member_names=tuple(
                            member_label(member, member_user) for member, member_user in members
                        ),
                        owner_name=owner_name,
                        owner_basis=owner_basis,
                        channel=(
                            "an iMessage group chat"
                            if source_channel == "linq"
                            else "a shared group chat"
                        ),
                    ),
                )
            if not relationship.empty and trigger is not None:
                modules.insert(
                    0,
                    build_relationship_module(
                        relationship,
                        latest_user_text=trigger.content,
                        now=trigger.created_at,
                    ),
                )
            if not memory.empty:
                modules.insert(0, build_memory_module(memory))
            instructions = build_benji_instructions(
                user,
                state_modules=tuple(modules),
                include_private_profile=(conversation.kind == ConversationKind.DIRECT.value),
            )
            if conversation.kind == ConversationKind.GROUP.value:
                # Group-safe capabilities belong to the shared conversation, not to the
                # owner's private onboarding state. Private tools stay excluded below.
                available_tools = tools.only(GROUP_SAFE_TOOL_NAMES)
            else:
                available_tools = (
                    tools
                    if user.onboarding_status == OnboardingStatus.COMPLETE.value
                    else ToolRegistry([])
                )
            prompt_hash = hashlib.sha256(instructions.encode("utf-8")).hexdigest()
            exposed_tools = [definition.name for definition in available_tools.definitions()]
            reasoning_effort = getattr(provider, "reasoning_effort", None)
            run = AgentRun(
                conversation_id=conversation_id,
                user_id=user_id,
                trigger_message_id=trigger_message_id,
                trigger_event_id=trigger_event_id,
                wake_type=wake_type,
                provider=provider.name,
                model=provider.model,
                purpose=purpose.value,
                input_message_count=len(messages),
                prompt_version=DOT_PROMPT_VERSION,
                prompt_hash=prompt_hash,
                prompt_snapshot={
                    "schema_version": 1,
                    "module_names": _PROMPT_MODULE_NAME_PATTERN.findall(instructions),
                    "instructions": instructions,
                },
                retrieved_memory=memory.trace_snapshot(),
                exposed_tools=exposed_tools,
                reasoning_effort=(reasoning_effort if isinstance(reasoning_effort, str) else None),
            )
            session.add(run)
            await session.commit()
            run_id = run.id

        result = await AgentRunner(
            provider=provider,
            tools=available_tools,
            max_tool_rounds=settings.agent_max_tool_rounds,
        ).run(
            instructions=instructions,
            messages=messages,
            context=ToolContext(
                user_id=user_id,
                conversation_id=conversation_id,
                agent_run_id=run_id,
                delivery_provider=delivery_provider,
            ),
        )

        async with factory() as session:
            run = await session.get(AgentRun, run_id)
            user = await session.get(User, user_id)
            if run is None or user is None:
                raise RuntimeError("Agent run or user no longer exists")
            if (
                conversation.kind == ConversationKind.DIRECT.value
                and result.language_preference is not None
            ):
                apply_language_preference(user=user, proposal=result.language_preference)
            if guard_follow_up_id is not None:
                follow_up = await session.get(AgentFollowUp, guard_follow_up_id)
                if follow_up is None or follow_up.status == AgentFollowUpStatus.CANCELLED.value:
                    raise AgentWakeCancelled("Follow-up was cancelled by a user message")
            existing_tool_calls = {
                call.external_call_id: call
                for call in (
                    await session.scalars(
                        select(AgentToolCall).where(AgentToolCall.agent_run_id == run.id)
                    )
                ).all()
            }
            for call in result.tool_calls:
                persisted_call = existing_tool_calls.get(call.call_id)
                if persisted_call is None:
                    session.add(
                        AgentToolCall(
                            agent_run_id=run.id,
                            external_call_id=call.call_id,
                            tool_name=call.name,
                            arguments=call.arguments,
                            output=call.output,
                            status=(
                                ToolCallStatus.COMPLETED.value
                                if call.succeeded
                                else ToolCallStatus.FAILED.value
                            ),
                        )
                    )
                    continue
                if (
                    persisted_call.tool_name != call.name
                    or persisted_call.arguments != call.arguments
                ):
                    raise RuntimeError(
                        "Tool-call identity was reused with different arguments"
                    )
                persisted_call.output = call.output
                persisted_call.status = (
                    ToolCallStatus.COMPLETED.value
                    if call.succeeded
                    else ToolCallStatus.FAILED.value
                )

            response_group_id = uuid4()
            replies: list[PersistedReply] = []
            persisted_messages: list[Message] = []
            clean_messages = (
                prepare_app_completion_bubbles(result.messages, app_url=required_app_url)
                if required_app_url is not None
                else prepare_text_bubbles(result.messages)
            )
            if not clean_messages:
                if purpose == AgentRunPurpose.EVENT and wake_type == "schedule.triggered":
                    run.status = AgentRunStatus.COMPLETED.value
                    run.model_response_id = result.response_id
                    run.raw_output = result.raw_output
                    run.token_usage = result.token_usage
                    run.completed_at = datetime.now(UTC)
                    await session.commit()
                    return PersistedTurn(replies=())
                raise RuntimeError("Agent returned an empty plain-text turn")
            for ordinal, text in enumerate(clean_messages):
                message_key = idempotency_key if ordinal == 0 else f"{idempotency_key}:{ordinal}"
                outbound = Message(
                    conversation_id=conversation_id,
                    user_id=user_id,
                    source_binding_id=source_binding_id,
                    source_channel=source_channel,
                    idempotency_key=message_key,
                    response_group_id=response_group_id,
                    response_ordinal=ordinal,
                    direction=MessageDirection.OUTBOUND.value,
                    status=MessageStatus.COMPLETED.value,
                    content=text,
                    raw_payload={
                        "agent_run_id": str(run.id),
                        "model_response_id": result.response_id,
                        "wake_type": wake_type,
                        "context_through_at": (
                            context_through.created_at.isoformat()
                            if context_through is not None
                            else None
                        ),
                        "context_message_ids": covered_inbound_ids,
                    },
                )
                session.add(outbound)
                persisted_messages.append(outbound)
            await session.flush()
            replies.extend(
                PersistedReply(message_id=message.id, text=message.content)
                for message in persisted_messages
            )

            if (
                settings.memory_enabled
                and trigger_message_id is not None
                and conversation.kind == ConversationKind.DIRECT.value
            ):
                await enqueue_memory_job(
                    session,
                    user_id=user_id,
                    conversation_id=conversation_id,
                    trigger_message_id=trigger_message_id,
                    response_message_id=persisted_messages[-1].id,
                    idempotency_key=f"benji:memory:{response_group_id}",
                )
            if (
                allow_follow_up
                and conversation.kind == ConversationKind.DIRECT.value
                and user.messaging_opted_out_at is None
            ):
                await schedule_follow_up(
                    session,
                    conversation_id=conversation_id,
                    user_id=user_id,
                    source_agent_run_id=run.id,
                    proposal=result.follow_up,
                    delivery_provider=delivery_provider,
                    chain_depth=chain_depth,
                    settings=settings,
                )
            run.status = AgentRunStatus.COMPLETED.value
            run.model_response_id = result.response_id
            run.raw_output = result.raw_output
            run.token_usage = result.token_usage
            run.completed_at = datetime.now(UTC)
            await session.commit()
            return PersistedTurn(replies=tuple(replies))
    except Exception as error:
        if run_id is not None:
            await mark_run_failed(run_id, error)
        raise


def _required_app_completion_url(
    *,
    event_type: str,
    payload: dict[str, object],
) -> str | None:
    if event_type != "app.build.completed":
        return None
    app_url = payload.get("app_url")
    if not isinstance(app_url, str) or not app_url.strip():
        raise RuntimeError("App completion event is missing its trusted app URL")
    return app_url


def _is_message_only_app_reminder(
    *,
    event_type: str,
    payload: dict[str, object],
) -> bool:
    """Keep app-authored reminder text out of Dot's capability-bearing agent loop."""
    return (
        event_type == "schedule.triggered"
        and payload.get("schedule_source") == "generated_app"
        and payload.get("tool_policy") == "message_only"
    )


async def _load_existing_turn(
    factory: async_sessionmaker[AsyncSession],
    source_channel: str,
    idempotency_key: str,
) -> PersistedTurn | None:
    async with factory() as session:
        messages = (
            await session.scalars(
                select(Message)
                .where(
                    Message.source_channel == source_channel,
                    or_(
                        Message.idempotency_key == idempotency_key,
                        Message.idempotency_key.like(f"{idempotency_key}:%"),
                    ),
                )
                .order_by(Message.created_at, Message.response_ordinal)
            )
        ).all()
        if not messages:
            return None
        return PersistedTurn(
            replies=tuple(
                PersistedReply(message_id=message.id, text=message.content) for message in messages
            )
        )


async def process_agent_turn(
    *,
    conversation_id: UUID,
    user_id: UUID,
    trigger_message_id: UUID,
    channel_id: UUID,
    chat_id: str,
    trigger_event_id: str,
    provider: ModelProvider,
    tools: ToolRegistry,
    linq_client: LinqClient,
    settings: Settings,
    embedding_provider: EmbeddingProvider | None = None,
    state_modules: tuple[PromptModule, ...] = (),
    allow_follow_up: bool = True,
    typing_enabled: bool = True,
) -> None:
    if typing_enabled:
        await set_typing(linq_client, chat_id=chat_id, active=True)
    idempotency_key = f"benji:{trigger_event_id}:agent"
    try:
        turn = await run_agent_turn(
            conversation_id=conversation_id,
            user_id=user_id,
            trigger_message_id=trigger_message_id,
            source_channel="linq",
            source_binding_id=channel_id,
            idempotency_key=idempotency_key,
            provider=provider,
            tools=tools,
            settings=settings,
            embedding_provider=embedding_provider,
            state_modules=state_modules,
            allow_follow_up=allow_follow_up,
        )
        await deliver_linq_replies(
            replies=turn.replies,
            channel_id=channel_id,
            chat_id=chat_id,
            idempotency_key=idempotency_key,
            client=linq_client,
            inter_message_delay_seconds=settings.agent_inter_bubble_delay_seconds,
            typing_between_messages=typing_enabled,
            typing_seconds_per_character=settings.agent_typing_seconds_per_character,
            typing_max_delay_seconds=settings.agent_typing_max_delay_seconds,
        )
    except Exception:
        logger.exception("Agent turn failed for conversation %s", conversation_id)
    finally:
        if typing_enabled:
            await set_typing(linq_client, chat_id=chat_id, active=False)
