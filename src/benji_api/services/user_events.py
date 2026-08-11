from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from benji_api.agents.channel_delivery import deliver_linq_replies, set_typing
from benji_api.agents.types import ModelProvider
from benji_api.config import Settings
from benji_api.db.session import async_session_factory
from benji_api.integrations.linq.client import LinqClient
from benji_api.memory.types import EmbeddingProvider
from benji_api.models.channel import Conversation, ConversationChannel, ConversationKind
from benji_api.models.user import User
from benji_api.models.user_event import UserEvent, UserEventStatus
from benji_api.services.channels import resolve_direct_conversation

if TYPE_CHECKING:
    from benji_api.agents.tools import ToolRegistry

logger = logging.getLogger(__name__)

_AGENT_EVENT_TYPES = {
    "integration.connected",
    "finance.connected",
    "finance.reauth_required",
    "group.benji_added",
    "group.dot_added",
    "schedule.triggered",
}


def register_agent_event_type(event_type: str) -> None:
    _AGENT_EVENT_TYPES.add(event_type)


async def enqueue_user_event(
    session: AsyncSession,
    *,
    user_id: UUID,
    conversation_id: UUID | None = None,
    event_type: str,
    source: str,
    idempotency_key: str,
    payload: dict[str, object],
    delivery_provider: str | None = None,
) -> UserEvent:
    """Add an event to the same database transaction as the state change."""
    existing = await session.scalar(
        select(UserEvent).where(UserEvent.idempotency_key == idempotency_key)
    )
    if existing is not None:
        return existing
    event = UserEvent(
        user_id=user_id,
        conversation_id=conversation_id,
        event_type=event_type,
        source=source,
        idempotency_key=idempotency_key,
        payload=payload,
        delivery_provider=delivery_provider,
    )
    session.add(event)
    await session.flush()
    return event


async def dispatch_user_event(
    *,
    settings: Settings,
    provider: ModelProvider | None,
    tools: ToolRegistry,
    linq_client: LinqClient | None,
    embedding_provider: EmbeddingProvider | None = None,
    event_id: UUID | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> bool:
    """Claim one event and let the agent continue the conversation from it."""
    factory = session_factory or async_session_factory
    event = await _claim_event(
        factory,
        max_attempts=settings.user_event_max_attempts,
        event_id=event_id,
    )
    if event is None:
        return False

    chat_id: str | None = None
    typing_started = False
    try:
        from benji_api.agents.service import run_agent_event

        if event.event_type not in _AGENT_EVENT_TYPES:
            await _finish_event(
                factory,
                event.id,
                status=UserEventStatus.SKIPPED,
                error=f"No agent event policy registered for {event.event_type}",
            )
            return True
        if provider is None:
            raise RuntimeError("The event agent model provider is not configured")
        if event.delivery_provider == "linq" and await _messaging_opted_out(factory, event.user_id):
            await _finish_event(
                factory,
                event.id,
                status=UserEventStatus.SKIPPED,
                error="User has opted out of proactive messaging",
            )
            return True

        conversation, channel = await _resolve_event_target(factory, event)
        if event.delivery_provider is not None and channel is None:
            await _finish_event(
                factory,
                event.id,
                status=UserEventStatus.SKIPPED,
                error=f"No active {event.delivery_provider} channel",
            )
            return True
        if event.delivery_provider == "linq":
            if not settings.linq_automated_replies_enabled:
                await _finish_event(
                    factory,
                    event.id,
                    status=UserEventStatus.SKIPPED,
                    error="Automated Linq delivery is disabled",
                )
                return True
            if linq_client is None or channel is None:
                raise RuntimeError("Linq delivery is not configured")
            chat_id = channel.external_id
            if (
                conversation.kind == ConversationKind.DIRECT.value
                and event.event_type != "schedule.triggered"
            ):
                await set_typing(linq_client, chat_id=chat_id, active=True)
                typing_started = True
        elif event.delivery_provider is not None:
            raise RuntimeError(f"No delivery adapter for {event.delivery_provider}")

        message_key = f"benji:user-event:{event.id}"
        turn = await run_agent_event(
            conversation_id=conversation.id,
            user_id=event.user_id,
            event_id=event.id,
            event_type=event.event_type,
            payload=event.payload,
            source_binding_id=channel.id if channel is not None else None,
            idempotency_key=message_key,
            delivery_provider=event.delivery_provider,
            provider=provider,
            tools=tools,
            settings=settings,
            embedding_provider=embedding_provider,
            session_factory=factory,
        )
        if event.delivery_provider == "linq" and channel is not None and chat_id is not None:
            await deliver_linq_replies(
                replies=turn.replies,
                channel_id=channel.id,
                chat_id=chat_id,
                idempotency_key=message_key,
                client=linq_client,
                inter_message_delay_seconds=settings.agent_inter_bubble_delay_seconds,
                typing_between_messages=(conversation.kind == ConversationKind.DIRECT.value),
                typing_seconds_per_character=settings.agent_typing_seconds_per_character,
                typing_max_delay_seconds=settings.agent_typing_max_delay_seconds,
            )
        await _finish_event(factory, event.id, status=UserEventStatus.PROCESSED)
    except Exception as error:
        logger.exception("User event %s failed", event.id)
        await _retry_event(factory, event.id, error)
    finally:
        if typing_started and linq_client is not None and chat_id is not None:
            await set_typing(linq_client, chat_id=chat_id, active=False)
    return True


async def _resolve_event_target(
    factory: async_sessionmaker[AsyncSession],
    event: UserEvent,
) -> tuple[Conversation, ConversationChannel | None]:
    async with factory() as session:
        if event.conversation_id is None:
            conversation = await resolve_direct_conversation(session, user_id=event.user_id)
        else:
            conversation = await session.get(Conversation, event.conversation_id)
            if conversation is None or conversation.user_id != event.user_id:
                raise RuntimeError("Event conversation target no longer exists")
        channel = None
        if event.delivery_provider is not None:
            channel = await session.scalar(
                select(ConversationChannel)
                .where(
                    ConversationChannel.conversation_id == conversation.id,
                    ConversationChannel.provider == event.delivery_provider,
                    ConversationChannel.status == "active",
                )
                .order_by(ConversationChannel.updated_at.desc())
                .limit(1)
            )
        await session.commit()
        return conversation, channel


async def _messaging_opted_out(
    factory: async_sessionmaker[AsyncSession],
    user_id: UUID,
) -> bool:
    async with factory() as session:
        user = await session.get(User, user_id)
        return user is None or user.messaging_opted_out_at is not None


async def _claim_event(
    factory: async_sessionmaker[AsyncSession],
    *,
    max_attempts: int,
    event_id: UUID | None,
) -> UserEvent | None:
    now = datetime.now(UTC)
    stale_before = now - timedelta(minutes=2)
    eligible = or_(
        UserEvent.status == UserEventStatus.PENDING.value,
        and_(
            UserEvent.status == UserEventStatus.FAILED.value,
            UserEvent.next_attempt_at <= now,
        ),
        and_(
            UserEvent.status == UserEventStatus.PROCESSING.value,
            UserEvent.locked_at <= stale_before,
        ),
    )
    async with factory() as session:
        statement = (
            select(UserEvent)
            .where(eligible, UserEvent.attempts < max_attempts)
            .order_by(UserEvent.next_attempt_at, UserEvent.created_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if event_id is not None:
            statement = statement.where(UserEvent.id == event_id)
        event = await session.scalar(statement)
        if event is None:
            return None
        event.status = UserEventStatus.PROCESSING.value
        event.attempts += 1
        event.locked_at = now
        event.error = None
        await session.commit()
        return event


async def _finish_event(
    factory: async_sessionmaker[AsyncSession],
    event_id: UUID,
    *,
    status: UserEventStatus,
    error: str | None = None,
) -> None:
    async with factory() as session:
        event = await session.get(UserEvent, event_id)
        if event is None:
            return
        event.status = status.value
        event.error = error
        event.locked_at = None
        event.processed_at = datetime.now(UTC)
        await session.commit()


async def _retry_event(
    factory: async_sessionmaker[AsyncSession],
    event_id: UUID,
    error: Exception,
) -> None:
    async with factory() as session:
        event = await session.get(UserEvent, event_id)
        if event is None:
            return
        delay_seconds = min(2 ** max(event.attempts, 1), 300)
        event.status = UserEventStatus.FAILED.value
        event.error = str(error)[:2_000]
        event.locked_at = None
        event.next_attempt_at = datetime.now(UTC) + timedelta(seconds=delay_seconds)
        await session.commit()
