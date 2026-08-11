import hashlib
import logging
import re
from datetime import UTC, datetime
from uuid import UUID, uuid4

from benji_api.agents.channel_delivery import (
    deliver_linq_replies,
    load_recent_messages,
    mark_run_failed,
    set_typing,
)
from benji_api.agents.locking import conversation_turn_lock
from benji_api.agents.prompts import build_benji_instructions
from benji_api.agents.prompts.base import DOT_PROMPT_VERSION
from benji_api.agents.prompts.onboarding import build_onboarding_module
from benji_api.agents.results import PersistedReply, PersistedTurn
from benji_api.agents.text_style import prepare_text_bubbles
from benji_api.agents.types import ModelProvider
from benji_api.config import Settings
from benji_api.db.session import async_session_factory
from benji_api.integrations.linq.client import LinqClient
from benji_api.memory.service import enqueue_memory_job
from benji_api.models.agent import AgentRun, AgentRunPurpose, AgentRunStatus
from benji_api.models.channel import (
    Conversation,
    Message,
    MessageDirection,
    MessageStatus,
)
from benji_api.models.user import User
from benji_api.services.language_preferences import apply_language_preference
from benji_api.services.onboarding import (
    ONBOARDING_OUTPUT,
    apply_profile_candidates,
    parse_onboarding_turn,
    validation_repair_reply,
)

logger = logging.getLogger(__name__)

_PROMPT_MODULE_NAME_PATTERN = re.compile(r'<prompt_module name="([^"]+)">')


async def run_onboarding_turn(
    *,
    conversation_id: UUID,
    user_id: UUID,
    trigger_message_id: UUID,
    is_new_user: bool,
    source_channel: str,
    source_binding_id: UUID,
    idempotency_key: str,
    provider: ModelProvider,
    settings: Settings,
) -> PersistedTurn:
    async with conversation_turn_lock(async_session_factory, conversation_id=conversation_id):
        return await _run_onboarding_turn_unlocked(
            conversation_id=conversation_id,
            user_id=user_id,
            trigger_message_id=trigger_message_id,
            is_new_user=is_new_user,
            source_channel=source_channel,
            source_binding_id=source_binding_id,
            idempotency_key=idempotency_key,
            provider=provider,
            settings=settings,
        )


async def _run_onboarding_turn_unlocked(
    *,
    conversation_id: UUID,
    user_id: UUID,
    trigger_message_id: UUID,
    is_new_user: bool,
    source_channel: str,
    source_binding_id: UUID,
    idempotency_key: str,
    provider: ModelProvider,
    settings: Settings,
) -> PersistedTurn:
    """Generate and persist onboarding without assuming a delivery channel."""
    run_id: UUID | None = None
    try:
        async with async_session_factory() as session:
            conversation = await session.get(Conversation, conversation_id)
            user = await session.get(User, user_id)
            if conversation is None or user is None:
                raise RuntimeError("Onboarding conversation or user no longer exists")

            messages = await load_recent_messages(
                session,
                conversation_id=conversation_id,
                limit=settings.agent_context_message_limit,
            )
            instructions = build_benji_instructions(
                user,
                state_modules=(build_onboarding_module(user, is_new_user=is_new_user),),
            )
            prompt_hash = hashlib.sha256(instructions.encode("utf-8")).hexdigest()
            reasoning_effort = getattr(provider, "reasoning_effort", None)
            run = AgentRun(
                conversation_id=conversation_id,
                user_id=user_id,
                trigger_message_id=trigger_message_id,
                provider=provider.name,
                model=provider.model,
                purpose=AgentRunPurpose.ONBOARDING.value,
                input_message_count=len(messages),
                prompt_version=DOT_PROMPT_VERSION,
                prompt_hash=prompt_hash,
                prompt_snapshot={
                    "schema_version": 1,
                    "module_names": _PROMPT_MODULE_NAME_PATTERN.findall(instructions),
                    "instructions": instructions,
                },
                retrieved_memory=[],
                exposed_tools=[],
                reasoning_effort=(
                    reasoning_effort if isinstance(reasoning_effort, str) else None
                ),
            )
            session.add(run)
            await session.commit()
            run_id = run.id

        result = await provider.generate_structured(
            instructions=instructions,
            messages=messages,
            output=ONBOARDING_OUTPUT,
        )
        turn = parse_onboarding_turn(result.data)

        async with async_session_factory() as session:
            user = await session.get(User, user_id)
            run = await session.get(AgentRun, run_id)
            if user is None or run is None:
                raise RuntimeError("Onboarding run or user no longer exists")

            if turn.language_preference is not None:
                apply_language_preference(user=user, proposal=turn.language_preference)
            profile_update = apply_profile_candidates(user=user, candidates=turn.profile)
            repair_reply = validation_repair_reply(profile_update.rejected_fields)
            texts = (
                (repair_reply,)
                if repair_reply is not None
                else prepare_text_bubbles(turn.messages)
            )
            if not texts:
                raise RuntimeError("Onboarding model returned an empty assistant turn")
            response_group_id = uuid4()
            replies: list[PersistedReply] = []
            for ordinal, text in enumerate(texts):
                outbound = Message(
                    conversation_id=conversation_id,
                    user_id=user_id,
                    source_binding_id=source_binding_id,
                    source_channel=source_channel,
                    idempotency_key=(
                        idempotency_key if ordinal == 0 else f"{idempotency_key}:{ordinal}"
                    ),
                    response_group_id=response_group_id,
                    response_ordinal=ordinal,
                    direction=MessageDirection.OUTBOUND.value,
                    status=MessageStatus.COMPLETED.value,
                    content=text,
                    raw_payload={
                        "agent_run_id": str(run.id),
                        "model_response_id": result.response_id,
                        "onboarding_completed": profile_update.completed,
                    },
                )
                session.add(outbound)
                await session.flush()
                replies.append(PersistedReply(message_id=outbound.id, text=text))
            if settings.memory_enabled:
                await enqueue_memory_job(
                    session,
                    user_id=user_id,
                    conversation_id=conversation_id,
                    trigger_message_id=trigger_message_id,
                    response_message_id=replies[-1].message_id,
                    idempotency_key=f"benji:memory:{response_group_id}",
                )
            run.status = AgentRunStatus.COMPLETED.value
            run.model_response_id = result.response_id
            run.raw_output = result.data
            run.token_usage = result.token_usage
            run.completed_at = datetime.now(UTC)
            await session.commit()
            return PersistedTurn(
                replies=tuple(replies),
                onboarding_completed=profile_update.completed,
            )
    except Exception as error:
        if run_id is not None:
            await mark_run_failed(run_id, error)
        raise


async def process_onboarding_turn(
    *,
    conversation_id: UUID,
    user_id: UUID,
    trigger_message_id: UUID,
    channel_id: UUID,
    chat_id: str,
    trigger_event_id: str,
    is_new_user: bool,
    share_contact_card: bool,
    provider: ModelProvider,
    linq_client: LinqClient,
    settings: Settings,
) -> None:
    """Run onboarding and deliver its persisted reply through Linq."""
    await set_typing(linq_client, chat_id=chat_id, active=True)
    idempotency_key = f"benji:{trigger_event_id}:onboarding"
    try:
        turn = await run_onboarding_turn(
            conversation_id=conversation_id,
            user_id=user_id,
            trigger_message_id=trigger_message_id,
            is_new_user=is_new_user,
            source_channel="linq",
            source_binding_id=channel_id,
            idempotency_key=idempotency_key,
            provider=provider,
            settings=settings,
        )
        await deliver_linq_replies(
            replies=turn.replies,
            channel_id=channel_id,
            chat_id=chat_id,
            idempotency_key=idempotency_key,
            client=linq_client,
            inter_message_delay_seconds=settings.agent_inter_bubble_delay_seconds,
            typing_between_messages=True,
            typing_seconds_per_character=settings.agent_typing_seconds_per_character,
            typing_max_delay_seconds=settings.agent_typing_max_delay_seconds,
        )

        if share_contact_card:
            try:
                await linq_client.share_contact_card(chat_id=chat_id)
            except Exception:
                logger.exception("Failed to share Linq contact card for event %s", trigger_event_id)
    except Exception:
        logger.exception("Onboarding turn failed for conversation %s", conversation_id)
    finally:
        await set_typing(linq_client, chat_id=chat_id, active=False)
