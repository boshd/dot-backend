from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from benji_api.models.channel import (
    Conversation,
    ConversationChannel,
    ConversationKind,
    ConversationMember,
    ConversationMemberRole,
    ConversationMemberStatus,
    DeliveryStatus,
    MessageDelivery,
)
from benji_api.services.users import get_primary_user_handle


class ChannelIdentityConflictError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ChannelResolution:
    conversation: Conversation
    channel: ConversationChannel


async def resolve_direct_conversation(
    session: AsyncSession,
    *,
    user_id: UUID,
    external_handle: str | None = None,
) -> Conversation:
    conversation = await session.scalar(
        select(Conversation).where(
            Conversation.user_id == user_id,
            Conversation.kind == ConversationKind.DIRECT.value,
        )
    )
    if conversation is None:
        conversation = Conversation(user_id=user_id, kind=ConversationKind.DIRECT.value)
        session.add(conversation)
        await session.flush()
        from benji_api.models.user import User

        user = await session.get(User, user_id)
        if user is None:
            raise RuntimeError("Direct conversation user no longer exists")
        member_handle = external_handle or await get_primary_user_handle(session, user)
        session.add(
            ConversationMember(
                conversation_id=conversation.id,
                user_id=user_id,
                external_handle=member_handle,
                role=ConversationMemberRole.OWNER.value,
                status=ConversationMemberStatus.ACTIVE.value,
            )
        )
        await session.flush()
    return conversation


async def resolve_channel_conversation(
    session: AsyncSession,
    *,
    user_id: UUID,
    provider: str,
    external_id: str,
    service: str | None,
    user_handle: str | None = None,
) -> ChannelResolution:
    channel = await session.scalar(
        select(ConversationChannel).where(
            ConversationChannel.provider == provider,
            ConversationChannel.external_id == external_id,
        )
    )
    if channel is not None:
        conversation = await session.get(Conversation, channel.conversation_id)
        if conversation is None or conversation.user_id != user_id:
            raise ChannelIdentityConflictError(
                "Channel identity is already attached to another user"
            )
        if service is not None:
            channel.service = service
        return ChannelResolution(conversation=conversation, channel=channel)

    conversation = await resolve_direct_conversation(
        session,
        user_id=user_id,
        external_handle=user_handle,
    )
    channel = ConversationChannel(
        conversation_id=conversation.id,
        provider=provider,
        external_id=external_id,
        service=service,
    )
    session.add(channel)
    await session.flush()
    return ChannelResolution(conversation=conversation, channel=channel)


async def apply_message_lifecycle_event(
    session: AsyncSession,
    *,
    provider: str,
    event_type: str,
    data: dict[str, object],
) -> bool:
    external_id = data.get("id")
    idempotency_key = data.get("idempotency_key")
    filters = []
    if external_id:
        filters.append(MessageDelivery.external_id == str(external_id))
    if idempotency_key:
        filters.append(MessageDelivery.idempotency_key == str(idempotency_key))
    if not filters:
        return False

    delivery = await session.scalar(
        select(MessageDelivery).where(
            MessageDelivery.provider == provider,
            or_(*filters),
        )
    )
    if delivery is None:
        return False

    if external_id and delivery.external_id is None:
        delivery.external_id = str(external_id)
    now = datetime.now(UTC)
    if event_type == "message.sent":
        _advance_delivery_status(delivery, DeliveryStatus.SENT)
        delivery.sent_at = delivery.sent_at or _event_datetime(data.get("sent_at")) or now
    elif event_type == "message.delivered":
        _advance_delivery_status(delivery, DeliveryStatus.DELIVERED)
        delivery.delivered_at = (
            delivery.delivered_at or _event_datetime(data.get("delivered_at")) or now
        )
    elif event_type == "message.read":
        _advance_delivery_status(delivery, DeliveryStatus.READ)
        delivery.read_at = delivery.read_at or _event_datetime(data.get("read_at")) or now
    elif event_type == "message.failed":
        if delivery.status not in {DeliveryStatus.DELIVERED.value, DeliveryStatus.READ.value}:
            delivery.status = DeliveryStatus.FAILED.value
    return True


def _advance_delivery_status(delivery: MessageDelivery, target: DeliveryStatus) -> None:
    order = {
        DeliveryStatus.PENDING.value: 0,
        DeliveryStatus.FAILED.value: 0,
        DeliveryStatus.SENT.value: 1,
        DeliveryStatus.DELIVERED.value: 2,
        DeliveryStatus.READ.value: 3,
    }
    if order.get(delivery.status, 0) <= order[target.value]:
        delivery.status = target.value


def _event_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
