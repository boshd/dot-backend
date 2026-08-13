import asyncio
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from benji_api.agents.media import model_attachment
from benji_api.agents.results import PersistedReply
from benji_api.agents.types import AgentMessage
from benji_api.db.session import async_session_factory
from benji_api.integrations.linq.client import LinqClient
from benji_api.models.agent import AgentRun, AgentRunStatus
from benji_api.models.channel import (
    Conversation,
    ConversationChannel,
    ConversationKind,
    DeliveryStatus,
    Message,
    MessageAttachment,
    MessageDelivery,
    MessageDirection,
)
from benji_api.models.user import User

logger = logging.getLogger(__name__)


async def load_recent_messages(
    session: AsyncSession,
    *,
    conversation_id: UUID,
    limit: int,
    through_message_id: UUID | None = None,
) -> list[AgentMessage]:
    conversation = await session.get(Conversation, conversation_id)
    if conversation is None:
        return []
    through = (
        await session.get(Message, through_message_id)
        if through_message_id is not None
        else None
    )
    if through_message_id is not None and (
        through is None or through.conversation_id != conversation_id
    ):
        return []
    message_filter = [
        Message.conversation_id == conversation_id,
        Message.content != "",
    ]
    if through is not None:
        # The trigger itself plus strictly older messages prevents a later persisted inbound
        # message from influencing a response or reaction aimed at this trigger.
        message_filter.append(
            or_(Message.created_at < through.created_at, Message.id == through.id)
        )
    result = await session.execute(
        select(Message, User)
        .outerjoin(User, User.id == Message.sender_user_id)
        .where(*message_filter)
        .order_by(Message.created_at.desc())
        .limit(limit)
    )
    rows = list(reversed(result.all()))
    attachment_rows = []
    message_ids = [message.id for message, _ in rows]
    if message_ids:
        attachment_rows = list(
            (
                await session.scalars(
                    select(MessageAttachment)
                    .where(MessageAttachment.message_id.in_(message_ids))
                    .order_by(
                        MessageAttachment.message_id,
                        MessageAttachment.part_index,
                    )
                )
            ).all()
        )
    attachments_by_message: dict[UUID, list[MessageAttachment]] = defaultdict(list)
    for attachment in attachment_rows:
        attachments_by_message[attachment.message_id].append(attachment)
    return [
        AgentMessage(
            role=("user" if message.direction == MessageDirection.INBOUND.value else "assistant"),
            content=(
                _group_message_content(message, sender)
                if conversation.kind == ConversationKind.GROUP.value
                and message.direction == MessageDirection.INBOUND.value
                else message.content
            ),
            attachments=tuple(
                model_attachment(attachment) for attachment in attachments_by_message[message.id]
            ),
        )
        for message, sender in rows
    ]


def _group_message_content(message: Message, sender: User | None) -> str:
    if sender is not None and sender.display_name:
        label = sender.display_name
    elif sender is not None and sender.phone_number:
        label = f"member ending {sender.phone_number[-4:]}"
    else:
        raw_label = message.raw_payload.get("_sender_label")
        label = raw_label if isinstance(raw_label, str) and raw_label else "group member"
    return f"[{label}]: {message.content}"


async def create_outbound_delivery(
    *,
    message_id: UUID,
    channel_id: UUID,
    provider: str,
    idempotency_key: str,
) -> UUID:
    async with async_session_factory() as session:
        existing = await session.scalar(
            select(MessageDelivery).where(
                MessageDelivery.provider == provider,
                MessageDelivery.idempotency_key == idempotency_key,
            )
        )
        if existing is not None:
            return existing.id

        message = await session.get(Message, message_id)
        channel = await session.get(ConversationChannel, channel_id)
        if message is None or channel is None or message.conversation_id != channel.conversation_id:
            raise RuntimeError("Message delivery channel does not match its conversation")

        delivery = MessageDelivery(
            message_id=message_id,
            channel_id=channel_id,
            provider=provider,
            idempotency_key=idempotency_key,
            status=DeliveryStatus.PENDING.value,
        )
        session.add(delivery)
        await session.commit()
        return delivery.id


async def deliver_linq_replies(
    *,
    replies: tuple[PersistedReply, ...],
    channel_id: UUID,
    chat_id: str,
    idempotency_key: str,
    client: LinqClient,
    inter_message_delay_seconds: float = 0,
    typing_between_messages: bool = False,
    typing_seconds_per_character: float = 0.022,
    typing_max_delay_seconds: float = 3.2,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    for ordinal, reply in enumerate(replies):
        delivery_key = idempotency_key if ordinal == 0 else f"{idempotency_key}:{ordinal}"
        delivery_id = await create_outbound_delivery(
            message_id=reply.message_id,
            channel_id=channel_id,
            provider="linq",
            idempotency_key=delivery_key,
        )
        if not await _delivery_needs_send(delivery_id):
            continue
        restarted_typing = ordinal > 0 and typing_between_messages
        if restarted_typing:
            await set_typing(client, chat_id=chat_id, active=True)
        delay = inter_bubble_typing_delay(
            reply.text,
            minimum_seconds=inter_message_delay_seconds,
            seconds_per_character=typing_seconds_per_character,
            maximum_seconds=typing_max_delay_seconds,
        )
        if ordinal and delay > 0:
            await sleep(delay)
        try:
            response = await client.send_chat_message(
                chat_id=chat_id,
                text=reply.text,
                idempotency_key=delivery_key,
            )
        except Exception as error:
            if restarted_typing:
                await set_typing(client, chat_id=chat_id, active=False)
            await mark_outbound_failed(delivery_id, error)
            raise
        await mark_outbound_sent(delivery_id, response)


async def deliver_linq_reaction(
    *,
    reaction_message_id: UUID,
    target_external_id: str,
    reaction_type: str,
    channel_id: UUID,
    idempotency_key: str,
    client: LinqClient,
) -> None:
    """Best-effort native reaction delivery; cosmetic failure never blocks text."""
    delivery_key = f"{idempotency_key}:reaction"
    delivery_id = await create_outbound_delivery(
        message_id=reaction_message_id,
        channel_id=channel_id,
        provider="linq",
        idempotency_key=delivery_key,
    )
    if not await _delivery_needs_send(delivery_id, retry_failed=False):
        return
    try:
        response = await client.add_message_reaction(
            message_id=target_external_id,
            reaction_type=reaction_type,
        )
    except Exception as error:
        await mark_outbound_failed(delivery_id, error)
        logger.warning(
            "Could not add Linq reaction to message %s",
            target_external_id,
            exc_info=True,
        )
        return
    await mark_outbound_sent(delivery_id, response)


def inter_bubble_typing_delay(
    text: str,
    *,
    minimum_seconds: float,
    seconds_per_character: float,
    maximum_seconds: float,
) -> float:
    """Return a short fake-typing pause; a zero minimum disables pacing for tests."""
    minimum = max(0.0, minimum_seconds)
    if minimum == 0:
        return 0.0
    character_delay = max(0.0, seconds_per_character) * len(text.strip())
    maximum = max(minimum, maximum_seconds)
    return min(maximum, minimum + character_delay)


async def _delivery_needs_send(
    delivery_id: UUID,
    *,
    retry_failed: bool = True,
) -> bool:
    async with async_session_factory() as session:
        delivery = await session.get(MessageDelivery, delivery_id)
        retryable = {DeliveryStatus.PENDING.value}
        if retry_failed:
            retryable.add(DeliveryStatus.FAILED.value)
        return delivery is None or delivery.status in retryable


async def mark_run_failed(run_id: UUID, error: Exception) -> None:
    async with async_session_factory() as session:
        run = await session.get(AgentRun, run_id)
        if run is None or run.status == AgentRunStatus.FAILED.value:
            return
        run.status = AgentRunStatus.FAILED.value
        run.error = str(error)[:2_000]
        run.completed_at = datetime.now(UTC)
        await session.commit()


async def mark_outbound_failed(delivery_id: UUID, error: Exception) -> None:
    async with async_session_factory() as session:
        delivery = await session.get(MessageDelivery, delivery_id)
        if delivery is None:
            return
        if delivery.status not in {DeliveryStatus.DELIVERED.value, DeliveryStatus.READ.value}:
            delivery.status = DeliveryStatus.FAILED.value
        delivery.raw_payload = {
            **delivery.raw_payload,
            "send_error": str(error)[:1_000],
        }
        await session.commit()


async def mark_outbound_sent(
    delivery_id: UUID,
    response: dict[str, object],
) -> None:
    async with async_session_factory() as session:
        delivery = await session.get(MessageDelivery, delivery_id)
        if delivery is None:
            return
        external_id = _extract_message_id(response)
        if external_id is not None:
            delivery.external_id = external_id
        if delivery.status in {DeliveryStatus.PENDING.value, DeliveryStatus.FAILED.value}:
            delivery.status = DeliveryStatus.SENT.value
        delivery.sent_at = delivery.sent_at or datetime.now(UTC)
        delivery.raw_payload = {**delivery.raw_payload, "linq_response": response}
        await session.commit()


async def set_typing(client: LinqClient, *, chat_id: str, active: bool) -> None:
    try:
        if active:
            await client.start_typing(chat_id=chat_id)
        else:
            await client.stop_typing(chat_id=chat_id)
    except Exception:
        logger.warning(
            "Could not %s Linq typing indicator for chat %s",
            "start" if active else "stop",
            chat_id,
            exc_info=True,
        )


def _extract_message_id(response: dict[str, object]) -> str | None:
    if response.get("id"):
        return str(response["id"])
    for key in ("message", "data"):
        nested = response.get(key)
        if isinstance(nested, dict) and nested.get("id"):
            return str(nested["id"])
    return None
