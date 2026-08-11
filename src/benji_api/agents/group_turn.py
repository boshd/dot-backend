import asyncio
import logging
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from benji_api.agents.channel_delivery import (
    deliver_linq_replies,
    load_recent_messages,
)
from benji_api.agents.group_participation import decide_group_participation
from benji_api.agents.locking import conversation_turn_lock
from benji_api.agents.prompts.base import PromptModule
from benji_api.agents.prompts.group import build_group_module
from benji_api.agents.results import PersistedReply
from benji_api.agents.service import run_agent_turn_unlocked
from benji_api.agents.tools import ToolRegistry
from benji_api.agents.types import ModelProvider
from benji_api.config import Settings
from benji_api.db.session import async_session_factory
from benji_api.integrations.linq.client import LinqClient
from benji_api.memory.types import EmbeddingProvider
from benji_api.models.channel import (
    Conversation,
    Message,
    MessageDirection,
    MessageStatus,
)
from benji_api.services.groups import (
    claim_group_owner,
    group_owner_context,
    list_conversation_members,
    member_label,
)

logger = logging.getLogger(__name__)


async def process_group_agent_turn(
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
    force_response: bool,
    embedding_provider: EmbeddingProvider | None = None,
) -> None:
    try:
        async with conversation_turn_lock(async_session_factory, conversation_id=conversation_id):
            async with async_session_factory() as session:
                trigger = await session.get(Message, trigger_message_id)
                conversation = await session.get(Conversation, conversation_id)
                if trigger is None or conversation is None:
                    return
                if await _trigger_was_covered(session, trigger):
                    return
                module = await _group_module(
                    session,
                    conversation=conversation,
                    current_speaker=_message_speaker(trigger),
                )
                messages = await load_recent_messages(
                    session,
                    conversation_id=conversation_id,
                    limit=settings.agent_context_message_limit,
                )

            decision = await decide_group_participation(
                provider=provider,
                messages=messages,
                group_module=module,
                force_response=force_response,
            )
            if not decision.should_respond:
                return

            async with async_session_factory() as session:
                conversation = await session.get(Conversation, conversation_id)
                trigger = await session.get(Message, trigger_message_id)
                if conversation is not None and trigger is not None:
                    members = await list_conversation_members(
                        session, conversation_id=conversation_id
                    )
                    speaker_member = next(
                        (
                            member
                            for member, _ in members
                            if (
                                trigger.sender_user_id is not None
                                and member.user_id == trigger.sender_user_id
                            )
                            or member.external_handle.casefold()
                            == str(
                                trigger.raw_payload.get("sender_handle", {}).get("handle", "")
                            ).casefold()
                        ),
                        None,
                    )
                    if speaker_member is not None:
                        await claim_group_owner(
                            session,
                            conversation=conversation,
                            member=speaker_member,
                        )
                        await session.commit()

            if decision.acknowledgment:
                await _persist_and_deliver_acknowledgment(
                    conversation_id=conversation_id,
                    user_id=user_id,
                    channel_id=channel_id,
                    chat_id=chat_id,
                    trigger_event_id=trigger_event_id,
                    acknowledgment=decision.acknowledgment,
                    client=linq_client,
                )
                if settings.agent_group_ack_settle_seconds > 0:
                    await asyncio.sleep(settings.agent_group_ack_settle_seconds)

            async with async_session_factory() as session:
                conversation = await session.get(Conversation, conversation_id)
                latest = await session.scalar(
                    select(Message)
                    .where(
                        Message.conversation_id == conversation_id,
                        Message.direction == MessageDirection.INBOUND.value,
                    )
                    .order_by(Message.created_at.desc(), Message.id.desc())
                    .limit(1)
                )
                if conversation is None or latest is None:
                    return
                module = await _group_module(
                    session,
                    conversation=conversation,
                    current_speaker=_message_speaker(latest),
                    acknowledgment=decision.acknowledgment,
                )

            idempotency_key = f"benji:{trigger_event_id}:agent"
            turn = await run_agent_turn_unlocked(
                conversation_id=conversation_id,
                user_id=user_id,
                trigger_message_id=latest.id,
                source_channel="linq",
                source_binding_id=channel_id,
                idempotency_key=idempotency_key,
                provider=provider,
                tools=tools,
                settings=settings,
                embedding_provider=embedding_provider,
                state_modules=(module,),
                allow_follow_up=False,
            )
            await deliver_linq_replies(
                replies=turn.replies,
                channel_id=channel_id,
                chat_id=chat_id,
                idempotency_key=idempotency_key,
                client=linq_client,
                inter_message_delay_seconds=settings.agent_inter_bubble_delay_seconds,
                typing_seconds_per_character=settings.agent_typing_seconds_per_character,
                typing_max_delay_seconds=settings.agent_typing_max_delay_seconds,
            )
    except Exception:
        logger.exception("Group agent turn failed for conversation %s", conversation_id)


async def _group_module(
    session: AsyncSession,
    *,
    conversation: Conversation,
    current_speaker: str,
    acknowledgment: str | None = None,
) -> PromptModule:
    members = await list_conversation_members(session, conversation_id=conversation.id)
    owner_name, owner_basis = group_owner_context(members, source=conversation.group_owner_source)
    return build_group_module(
        title=conversation.title or "group with dot",
        current_speaker=current_speaker,
        member_names=tuple(member_label(member, user) for member, user in members),
        owner_name=owner_name,
        owner_basis=owner_basis,
        channel="an iMessage group chat through Linq",
        preliminary_acknowledgment=acknowledgment,
    )


async def _trigger_was_covered(session: AsyncSession, trigger: Message) -> bool:
    recent_outbound = (
        await session.scalars(
            select(Message)
            .where(
                Message.conversation_id == trigger.conversation_id,
                Message.direction == MessageDirection.OUTBOUND.value,
            )
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(12)
        )
    ).all()
    for message in recent_outbound:
        covered_ids = message.raw_payload.get("context_message_ids")
        if isinstance(covered_ids, list) and str(trigger.id) in covered_ids:
            return True
        raw_value = message.raw_payload.get("context_through_at")
        if not isinstance(raw_value, str):
            continue
        try:
            covered_at = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
        except ValueError:
            continue
        if _as_utc(covered_at) > _as_utc(trigger.created_at):
            return True
    return False


async def _persist_and_deliver_acknowledgment(
    *,
    conversation_id: UUID,
    user_id: UUID,
    channel_id: UUID,
    chat_id: str,
    trigger_event_id: str,
    acknowledgment: str,
    client: LinqClient,
) -> None:
    idempotency_key = f"benji:{trigger_event_id}:ack"
    async with async_session_factory() as session:
        existing = await session.scalar(
            select(Message).where(
                Message.source_channel == "linq",
                Message.idempotency_key == idempotency_key,
            )
        )
        if existing is None:
            existing = Message(
                conversation_id=conversation_id,
                user_id=user_id,
                source_binding_id=channel_id,
                source_channel="linq",
                idempotency_key=idempotency_key,
                response_group_id=uuid4(),
                response_ordinal=0,
                direction=MessageDirection.OUTBOUND.value,
                status=MessageStatus.COMPLETED.value,
                content=acknowledgment,
                raw_payload={"preliminary_acknowledgment": True},
            )
            session.add(existing)
            await session.commit()
    await deliver_linq_replies(
        replies=(PersistedReply(message_id=existing.id, text=existing.content),),
        channel_id=channel_id,
        chat_id=chat_id,
        idempotency_key=idempotency_key,
        client=client,
    )


def _message_speaker(message: Message) -> str:
    label = message.raw_payload.get("_sender_label")
    return label if isinstance(label, str) and label else "an unnamed group member"


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
