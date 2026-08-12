from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from benji_api.agents.channel_delivery import deliver_linq_replies, set_typing
from benji_api.agents.locking import conversation_turn_lock
from benji_api.agents.results import PersistedReply, PersistedTurn
from benji_api.agents.types import ModelProvider
from benji_api.config import Settings
from benji_api.db.session import async_session_factory
from benji_api.integrations.linq.client import LinqClient
from benji_api.memory.types import EmbeddingProvider
from benji_api.models.channel import (
    Conversation,
    ConversationChannel,
    ConversationKind,
    Message,
    MessageDirection,
    MessageStatus,
)
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
    "app.build.completed",
    "app.build.failed",
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
    conversation: Conversation | None = None
    channel: ConversationChannel | None = None
    turn: PersistedTurn | None = None
    delivery_ready = False
    delivery_note: str | None = None
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
        conversation, channel = await _resolve_event_target(factory, event)
        if event.delivery_provider == "linq":
            if (
                conversation.kind == ConversationKind.DIRECT.value
                and await _messaging_opted_out(factory, event.user_id)
            ):
                delivery_note = "User has opted out of proactive messaging"
            elif channel is None:
                delivery_note = "No active linq channel"
            elif not settings.linq_automated_replies_enabled:
                delivery_note = "Automated Linq delivery is disabled"
            elif linq_client is None:
                delivery_note = "Linq delivery is not configured"
            else:
                delivery_ready = True
                chat_id = channel.external_id
                if (
                    conversation.kind == ConversationKind.DIRECT.value
                    and event.event_type != "schedule.triggered"
                ):
                    await set_typing(linq_client, chat_id=chat_id, active=True)
                    typing_started = True
        elif event.delivery_provider is not None:
            raise RuntimeError(f"No delivery adapter for {event.delivery_provider}")
        if provider is None:
            raise RuntimeError("The event agent model provider is not configured")

        message_key = f"benji:user-event:{event.id}"
        turn = await run_agent_event(
            conversation_id=conversation.id,
            user_id=event.user_id,
            event_id=event.id,
            event_type=event.event_type,
            payload=event.payload,
            source_binding_id=channel.id if channel is not None else None,
            idempotency_key=message_key,
            delivery_provider=(event.delivery_provider if delivery_ready else None),
            provider=provider,
            tools=tools,
            settings=settings,
            embedding_provider=embedding_provider,
            session_factory=factory,
        )
        if (
            delivery_ready
            and channel is not None
            and chat_id is not None
            and linq_client is not None
        ):
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
        await _finish_event(
            factory,
            event.id,
            status=UserEventStatus.PROCESSED,
            error=delivery_note,
        )
    except Exception as error:
        logger.exception("User event %s failed", event.id)
        exhausted = await _retry_event(
            factory,
            event.id,
            error,
            max_attempts=settings.user_event_max_attempts,
        )
        if exhausted and event.event_type in {"app.build.completed", "app.build.failed"}:
            try:
                if conversation is None:
                    conversation, channel = await _resolve_event_target(factory, event)
                message_key = f"benji:user-event:{event.id}"
                fallback_turn = (
                    await _ensure_app_completion_turn(
                        factory,
                        event=event,
                        conversation=conversation,
                        source_binding_id=channel.id if channel is not None else None,
                        idempotency_key=message_key,
                    )
                    if event.event_type == "app.build.completed"
                    else await _ensure_app_failure_turn(
                        factory,
                        event=event,
                        conversation=conversation,
                        source_binding_id=channel.id if channel is not None else None,
                        idempotency_key=message_key,
                    )
                )
                if (
                    turn is None
                    and delivery_ready
                    and channel is not None
                    and chat_id is not None
                    and linq_client is not None
                ):
                    try:
                        await deliver_linq_replies(
                            replies=fallback_turn.replies,
                            channel_id=channel.id,
                            chat_id=chat_id,
                            idempotency_key=message_key,
                            client=linq_client,
                            inter_message_delay_seconds=(
                                settings.agent_inter_bubble_delay_seconds
                            ),
                            typing_between_messages=(
                                conversation.kind == ConversationKind.DIRECT.value
                            ),
                            typing_seconds_per_character=(
                                settings.agent_typing_seconds_per_character
                            ),
                            typing_max_delay_seconds=settings.agent_typing_max_delay_seconds,
                        )
                    except Exception:
                        logger.exception(
                            "Canonical app completion %s persisted but Linq delivery failed",
                            event.id,
                        )
                await _finish_event(
                    factory,
                    event.id,
                    status=UserEventStatus.PROCESSED,
                    error=(
                        "Agent generation exhausted retries; persisted the canonical app "
                        "completion instead"
                    ),
                )
            except Exception:
                logger.exception("Could not persist canonical app completion %s", event.id)
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
            if conversation is None or conversation.status != "active":
                raise RuntimeError("Event conversation target no longer exists")
            # A direct conversation is permanently owned by one user. Group ownership can
            # legitimately transfer while durable work is in flight, so the trusted
            # conversation id remains the delivery authority for a group event.
            if (
                conversation.kind == ConversationKind.DIRECT.value
                and conversation.user_id != event.user_id
            ):
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
    *,
    max_attempts: int,
) -> bool:
    async with factory() as session:
        event = await session.get(UserEvent, event_id)
        if event is None:
            return False
        delay_seconds = min(2 ** max(event.attempts, 1), 300)
        event.status = UserEventStatus.FAILED.value
        event.error = str(error)[:2_000]
        event.locked_at = None
        event.next_attempt_at = datetime.now(UTC) + timedelta(seconds=delay_seconds)
        await session.commit()
        return event.attempts >= max_attempts


async def _ensure_app_completion_turn(
    factory: async_sessionmaker[AsyncSession],
    *,
    event: UserEvent,
    conversation: Conversation,
    source_binding_id: UUID | None,
    idempotency_key: str,
) -> PersistedTurn:
    app_url = event.payload.get("app_url")
    if not isinstance(app_url, str) or not app_url.strip():
        raise RuntimeError("App completion event is missing its trusted app URL")

    async with (
        conversation_turn_lock(factory, conversation_id=conversation.id),
        factory() as session,
    ):
        messages = list(
            (
                await session.scalars(
                    select(Message)
                    .where(
                        Message.source_channel == "event",
                        or_(
                            Message.idempotency_key == idempotency_key,
                            Message.idempotency_key.like(f"{idempotency_key}:%"),
                        ),
                    )
                    .order_by(Message.created_at, Message.response_ordinal)
                    .with_for_update()
                )
            ).all()
        )
        if not any(app_url in message.content for message in messages):
            response_group_id = (
                messages[0].response_group_id
                if messages and messages[0].response_group_id is not None
                else uuid4()
            )
            ordinal = max(
                (message.response_ordinal or 0 for message in messages),
                default=-1,
            ) + 1
            trusted_key = idempotency_key if not messages else f"{idempotency_key}:trusted-link"
            fallback = next(
                (message for message in messages if message.idempotency_key == trusted_key),
                None,
            )
            if fallback is None:
                fallback = Message(
                    conversation_id=conversation.id,
                    user_id=event.user_id,
                    source_binding_id=source_binding_id,
                    source_channel="event",
                    idempotency_key=trusted_key,
                    response_group_id=response_group_id,
                    response_ordinal=ordinal,
                    direction=MessageDirection.OUTBOUND.value,
                    status=MessageStatus.COMPLETED.value,
                    content=app_url,
                    raw_payload={
                        "wake_type": "app.build.completed",
                        "trusted_app_url": app_url,
                        "canonical_fallback": True,
                    },
                )
                session.add(fallback)
                await session.flush()
                messages.append(fallback)
            else:
                fallback.content = app_url
                fallback.raw_payload = {
                    **fallback.raw_payload,
                    "trusted_app_url": app_url,
                    "canonical_fallback": True,
                }
        await session.commit()
        return PersistedTurn(
            replies=tuple(
                PersistedReply(message_id=message.id, text=message.content)
                for message in messages
            )
        )


async def _ensure_app_failure_turn(
    factory: async_sessionmaker[AsyncSession],
    *,
    event: UserEvent,
    conversation: Conversation,
    source_binding_id: UUID | None,
    idempotency_key: str,
) -> PersistedTurn:
    title = event.payload.get("title")
    app_label = title.strip() if isinstance(title, str) and title.strip() else "that app"
    text = (
        f"i couldn't get {app_label} working cleanly, so i didn't send you a broken link. "
        "tell me what matters most and i'll take another run at it."
    )
    async with (
        conversation_turn_lock(factory, conversation_id=conversation.id),
        factory() as session,
    ):
        existing = await session.scalar(
            select(Message).where(
                Message.source_channel == "event",
                Message.idempotency_key == idempotency_key,
            )
        )
        if existing is None:
            existing = Message(
                conversation_id=conversation.id,
                user_id=event.user_id,
                source_binding_id=source_binding_id,
                source_channel="event",
                idempotency_key=idempotency_key,
                response_group_id=uuid4(),
                response_ordinal=0,
                direction=MessageDirection.OUTBOUND.value,
                status=MessageStatus.COMPLETED.value,
                content=text,
                raw_payload={
                    "wake_type": "app.build.failed",
                    "canonical_fallback": True,
                },
            )
            session.add(existing)
            await session.flush()
        await session.commit()
        return PersistedTurn(
            replies=(PersistedReply(message_id=existing.id, text=existing.content),)
        )
