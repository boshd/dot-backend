import json
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from benji_api.agents.dependencies import get_model_provider, get_tool_registry
from benji_api.agents.followups import cancel_pending_follow_ups
from benji_api.agents.group_turn import process_group_agent_turn
from benji_api.agents.onboarding import process_onboarding_turn
from benji_api.agents.service import process_agent_turn
from benji_api.agents.tools import ToolRegistry
from benji_api.agents.types import ModelProvider
from benji_api.config import Settings, get_settings
from benji_api.db.session import get_session
from benji_api.integrations.linq.client import LinqClient
from benji_api.integrations.linq.dependencies import get_linq_client
from benji_api.integrations.linq.schemas import LinqInboundMessage, LinqWebhookEnvelope
from benji_api.integrations.linq.webhooks import (
    LinqWebhookVerificationError,
    verify_linq_webhook,
)
from benji_api.memory.dependencies import get_embedding_provider
from benji_api.memory.types import EmbeddingProvider
from benji_api.models.channel import (
    Conversation,
    ConversationChannel,
    Message,
    MessageDelivery,
    MessageDirection,
    MessageStatus,
    WebhookEvent,
    WebhookStatus,
)
from benji_api.models.user import OnboardingStatus, User
from benji_api.models.user_event import UserEvent
from benji_api.schemas.phone import normalize_phone_number
from benji_api.services.channels import (
    apply_message_lifecycle_event,
    resolve_channel_conversation,
)
from benji_api.services.groups import (
    apply_linq_group_event,
    claim_group_owner,
    group_message_addresses_benji,
    list_conversation_members,
    member_label,
    resolve_linq_group_conversation,
    sync_linq_group_participants,
)
from benji_api.services.onboarding import apply_messaging_preference
from benji_api.services.user_events import dispatch_user_event, enqueue_user_event
from benji_api.services.users import resolve_user_from_phone

router = APIRouter(prefix="/webhooks/linq", tags=["linq webhooks"])
PROVIDER = "linq"
MESSAGE_LIFECYCLE_EVENTS = {
    "message.sent",
    "message.delivered",
    "message.read",
    "message.failed",
}
GROUP_STATE_EVENTS = {
    "participant.added",
    "participant.removed",
    "chat.group_name_updated",
    "chat.group_icon_updated",
}


class LinqWebhookReceipt(BaseModel):
    accepted: bool = True
    duplicate: bool = False
    event_id: str
    user_id: UUID | None = None
    reply_scheduled: bool = False


@router.post("", response_model=LinqWebhookReceipt)
async def receive_linq_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    linq_client: Annotated[LinqClient | None, Depends(get_linq_client)],
    model_provider: Annotated[ModelProvider | None, Depends(get_model_provider)],
    tool_registry: Annotated[ToolRegistry, Depends(get_tool_registry)],
    embedding_provider: Annotated[EmbeddingProvider | None, Depends(get_embedding_provider)],
) -> LinqWebhookReceipt:
    body = await request.body()
    if settings.linq_webhook_secret is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Linq webhook verification is not configured",
        )

    try:
        verify_linq_webhook(
            secret=settings.linq_webhook_secret,
            body=body,
            headers=request.headers,
        )
    except LinqWebhookVerificationError as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid webhook signature",
        ) from error

    try:
        payload = json.loads(body)
        envelope = LinqWebhookEnvelope.model_validate(payload)
    except (json.JSONDecodeError, ValidationError) as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid Linq webhook payload",
        ) from error

    existing_event = await session.scalar(
        select(WebhookEvent).where(
            WebhookEvent.provider == PROVIDER,
            WebhookEvent.external_event_id == envelope.event_id,
        )
    )
    if existing_event is not None:
        return LinqWebhookReceipt(event_id=envelope.event_id, duplicate=True)

    webhook_event = WebhookEvent(
        provider=PROVIDER,
        external_event_id=envelope.event_id,
        event_type=envelope.event_type,
        trace_id=envelope.trace_id,
        payload=payload,
    )
    session.add(webhook_event)

    if envelope.event_type in MESSAGE_LIFECYCLE_EVENTS:
        updated = await apply_message_lifecycle_event(
            session,
            provider=PROVIDER,
            event_type=envelope.event_type,
            data=envelope.data,
        )
        webhook_event.status = (
            WebhookStatus.PROCESSED.value if updated else WebhookStatus.IGNORED.value
        )
        webhook_event.processed_at = datetime.now(UTC)
        await session.commit()
        return LinqWebhookReceipt(event_id=envelope.event_id)

    if envelope.event_type == "chat.created":
        group_target = await _handle_linq_chat_created(session, envelope.data)
        handled = group_target is not None
        reply_scheduled = False
        target_user_id = None
        if group_target is not None:
            conversation, _ = group_target
            target_user_id = conversation.user_id
            event = await _enqueue_group_welcome(
                session,
                conversation=conversation,
                external_event_id=envelope.event_id,
            )
            reply_scheduled = bool(
                model_provider is not None
                and linq_client is not None
                and settings.linq_automated_replies_enabled
            )
            if reply_scheduled:
                background_tasks.add_task(
                    dispatch_user_event,
                    settings=settings,
                    provider=model_provider,
                    tools=tool_registry,
                    linq_client=linq_client,
                    embedding_provider=embedding_provider,
                    event_id=event.id,
                )
        webhook_event.status = (
            WebhookStatus.PROCESSED.value if handled else WebhookStatus.IGNORED.value
        )
        webhook_event.processed_at = datetime.now(UTC)
        await session.commit()
        return LinqWebhookReceipt(
            event_id=envelope.event_id,
            user_id=target_user_id,
            reply_scheduled=reply_scheduled,
        )

    if envelope.event_type in GROUP_STATE_EVENTS:
        group_target = None
        dot_was_added = envelope.event_type == "participant.added" and _participant_is_dot(
            envelope.data, settings=settings
        )
        if dot_was_added:
            group_target = await _resolve_group_from_linq_event(
                session,
                linq_client=linq_client,
                data=envelope.data,
            )
        handled = await apply_linq_group_event(
            session,
            event_type=envelope.event_type,
            data=envelope.data,
        )
        if group_target is None:
            group_target = await _load_group_target(session, data=envelope.data)
        reply_scheduled = False
        target_user_id = None
        if handled and dot_was_added and group_target is not None:
            conversation, _ = group_target
            target_user_id = conversation.user_id
            event = await _enqueue_group_welcome(
                session,
                conversation=conversation,
                external_event_id=envelope.event_id,
            )
            reply_scheduled = bool(
                model_provider is not None
                and linq_client is not None
                and settings.linq_automated_replies_enabled
            )
            if reply_scheduled:
                background_tasks.add_task(
                    dispatch_user_event,
                    settings=settings,
                    provider=model_provider,
                    tools=tool_registry,
                    linq_client=linq_client,
                    embedding_provider=embedding_provider,
                    event_id=event.id,
                )
        webhook_event.status = (
            WebhookStatus.PROCESSED.value if handled else WebhookStatus.IGNORED.value
        )
        webhook_event.processed_at = datetime.now(UTC)
        await session.commit()
        return LinqWebhookReceipt(
            event_id=envelope.event_id,
            user_id=target_user_id,
            reply_scheduled=reply_scheduled,
        )

    if envelope.event_type != "message.received":
        webhook_event.status = WebhookStatus.IGNORED.value
        webhook_event.processed_at = datetime.now(UTC)
        await session.commit()
        return LinqWebhookReceipt(event_id=envelope.event_id)

    try:
        inbound = LinqInboundMessage.from_envelope(envelope)
    except (KeyError, TypeError, ValidationError, ValueError) as error:
        webhook_event.status = WebhookStatus.FAILED.value
        webhook_event.error = str(error)[:2_000]
        webhook_event.processed_at = datetime.now(UTC)
        await session.commit()
        return LinqWebhookReceipt(event_id=envelope.event_id)

    existing_message = await session.scalar(
        select(Message).where(
            Message.source_channel == PROVIDER,
            Message.source_external_id == inbound.external_message_id,
        )
    )
    if existing_message is not None:
        webhook_event.status = WebhookStatus.PROCESSED.value
        webhook_event.processed_at = datetime.now(UTC)
        await session.commit()
        return LinqWebhookReceipt(
            event_id=envelope.event_id,
            duplicate=True,
            user_id=existing_message.user_id,
        )

    group_created = False
    group_can_reply = True
    sender_user_id = None
    sender_created = False
    speaker_label = "a group member"
    group_directly_addressed = False
    if inbound.is_group:
        chat_data = await _load_linq_chat_data(linq_client, inbound.external_chat_id)
        try:
            resolution = await resolve_user_from_phone(session, inbound.sender_handle)
        except ValueError:
            resolution = None
        if resolution is not None:
            actor_user = resolution.user
            sender_user_id = actor_user.id
            conversation, channel, group_created = await resolve_linq_group_conversation(
                session,
                external_chat_id=inbound.external_chat_id,
                sender=actor_user,
                service=inbound.service,
                chat_data=chat_data,
                claim_owner=False,
            )
        else:
            target = await _resolve_group_from_linq_event(
                session,
                linq_client=linq_client,
                data={"chat_id": inbound.external_chat_id},
            )
            if target is None:
                webhook_event.status = WebhookStatus.FAILED.value
                webhook_event.error = "Group sender identity could not be resolved"
                webhook_event.processed_at = datetime.now(UTC)
                await session.commit()
                return LinqWebhookReceipt(event_id=envelope.event_id)
            conversation, channel = target
            if chat_data is not None:
                await sync_linq_group_participants(
                    session,
                    conversation=conversation,
                    chat_data=chat_data,
                )
            actor_user = await session.get(User, conversation.user_id)
            if actor_user is None:
                raise RuntimeError("Group owner no longer exists")

        members = await list_conversation_members(session, conversation_id=conversation.id)
        speaker_member = next(
            (
                (member, member_user)
                for member, member_user in members
                if (
                    member_user is not None
                    and sender_user_id is not None
                    and member_user.id == sender_user_id
                )
                or member.external_handle.casefold() == inbound.sender_handle.strip().casefold()
            ),
            None,
        )
        if speaker_member is not None:
            speaker_label = member_label(*speaker_member)
            if speaker_member[1] is not None:
                sender_user_id = speaker_member[1].id
            group_directly_addressed = bool(
                group_message_addresses_benji(inbound.text)
                or await _is_reply_to_benji(
                    session,
                    conversation_id=conversation.id,
                    external_message_id=inbound.reply_to_message_id,
                )
            )
            if group_directly_addressed:
                await claim_group_owner(
                    session,
                    conversation=conversation,
                    member=speaker_member[0],
                )
        group_can_reply = _linq_chat_has_active_dot(chat_data)
        conversation.status = "active" if group_can_reply else "inactive"
        channel.status = "active" if group_can_reply else "inactive"
    else:
        try:
            resolution = await resolve_user_from_phone(session, inbound.sender_handle)
        except ValueError:
            webhook_event.status = WebhookStatus.FAILED.value
            webhook_event.error = "Direct sender is not linked to a phone identity"
            webhook_event.processed_at = datetime.now(UTC)
            await session.commit()
            return LinqWebhookReceipt(event_id=envelope.event_id)
        actor_user = resolution.user
        sender_user_id = actor_user.id
        sender_created = resolution.created
        channel_resolution = await resolve_channel_conversation(
            session,
            user_id=actor_user.id,
            provider=PROVIDER,
            external_id=inbound.external_chat_id,
            service=inbound.service,
        )
        conversation = channel_resolution.conversation
        channel = channel_resolution.channel
    had_group_activity = bool(
        inbound.is_group
        and await session.scalar(
            select(Message.id).where(
                Message.conversation_id == conversation.id,
            )
        )
    )
    has_group_welcome = bool(
        inbound.is_group
        and await session.scalar(
            select(UserEvent.id).where(
                UserEvent.conversation_id == conversation.id,
                UserEvent.event_type.in_(("group.dot_added", "group.benji_added")),
            )
        )
    )
    inbound_message = Message(
        conversation_id=conversation.id,
        user_id=actor_user.id,
        sender_user_id=sender_user_id,
        source_binding_id=channel.id,
        source_channel=PROVIDER,
        source_external_id=inbound.external_message_id,
        direction=MessageDirection.INBOUND.value,
        status=MessageStatus.RECEIVED.value,
        content=inbound.text,
        raw_payload={**envelope.data, "_sender_label": speaker_label},
        created_at=envelope.created_at,
    )
    session.add(inbound_message)
    conversation.updated_at = datetime.now(UTC)
    await session.flush()
    await cancel_pending_follow_ups(session, conversation_id=conversation.id)

    messaging_preference = None
    if not inbound.is_group:
        messaging_preference = apply_messaging_preference(
            user=actor_user,
            text=inbound.text,
        )
        if messaging_preference.opted_out:
            channel.status = "opted_out"
        elif channel.status == "opted_out":
            channel.status = "active"

    onboarding_reply_scheduled = bool(
        not inbound.is_group
        and messaging_preference is not None
        and not messaging_preference.opted_out
        and actor_user.onboarding_status != OnboardingStatus.COMPLETE.value
        and model_provider is not None
        and linq_client is not None
        and settings.linq_automated_replies_enabled
    )
    if onboarding_reply_scheduled and model_provider is not None and linq_client is not None:
        background_tasks.add_task(
            process_onboarding_turn,
            conversation_id=conversation.id,
            user_id=actor_user.id,
            trigger_message_id=inbound_message.id,
            channel_id=channel.id,
            chat_id=inbound.external_chat_id,
            trigger_event_id=envelope.event_id,
            is_new_user=sender_created,
            share_contact_card=sender_created and settings.linq_share_contact_card_enabled,
            provider=model_provider,
            linq_client=linq_client,
            settings=settings,
        )

    group_force_response = bool(
        inbound.is_group
        and group_can_reply
        and (
            group_created
            or (not had_group_activity and not has_group_welcome)
            or conversation.response_mode == "always"
            or group_directly_addressed
        )
    )
    group_should_reply = bool(
        inbound.is_group
        and group_can_reply
        and (group_force_response or conversation.response_mode in {"auto", "always"})
    )
    agent_reply_scheduled = bool(
        (
            group_should_reply
            or (
                not inbound.is_group
                and messaging_preference is not None
                and not messaging_preference.opted_out
                and actor_user.onboarding_status == OnboardingStatus.COMPLETE.value
            )
        )
        and not onboarding_reply_scheduled
        and model_provider is not None
        and linq_client is not None
        and settings.linq_automated_replies_enabled
    )
    if agent_reply_scheduled and model_provider is not None and linq_client is not None:
        turn_tools = tool_registry
        if inbound.is_group and sender_user_id is None:
            turn_tools = tool_registry.only(
                {"get_current_datetime", "search_web", "create_personal_app"}
            )
        if inbound.is_group:
            background_tasks.add_task(
                process_group_agent_turn,
                conversation_id=conversation.id,
                user_id=actor_user.id,
                trigger_message_id=inbound_message.id,
                channel_id=channel.id,
                chat_id=inbound.external_chat_id,
                trigger_event_id=envelope.event_id,
                provider=model_provider,
                tools=turn_tools,
                linq_client=linq_client,
                settings=settings,
                embedding_provider=embedding_provider,
                force_response=group_force_response,
            )
        else:
            background_tasks.add_task(
                process_agent_turn,
                conversation_id=conversation.id,
                user_id=actor_user.id,
                trigger_message_id=inbound_message.id,
                channel_id=channel.id,
                chat_id=inbound.external_chat_id,
                trigger_event_id=envelope.event_id,
                provider=model_provider,
                tools=turn_tools,
                linq_client=linq_client,
                settings=settings,
                embedding_provider=embedding_provider,
                allow_follow_up=True,
                typing_enabled=True,
            )

    webhook_event.status = WebhookStatus.PROCESSED.value
    webhook_event.processed_at = datetime.now(UTC)
    await session.commit()
    return LinqWebhookReceipt(
        event_id=envelope.event_id,
        user_id=actor_user.id,
        reply_scheduled=onboarding_reply_scheduled or agent_reply_scheduled,
    )


async def _load_linq_chat_data(
    linq_client: LinqClient | None,
    chat_id: str,
) -> dict[str, object] | None:
    if linq_client is None:
        return None
    try:
        response = await linq_client.get_chat(chat_id=chat_id)
    except Exception:
        return None
    for key in ("chat", "data"):
        nested = response.get(key)
        if isinstance(nested, dict):
            return nested
    return response


async def _enqueue_group_welcome(
    session: AsyncSession,
    *,
    conversation: Conversation,
    external_event_id: str,
) -> UserEvent:
    members = await list_conversation_members(session, conversation_id=conversation.id)
    return await enqueue_user_event(
        session,
        user_id=conversation.user_id,
        conversation_id=conversation.id,
        event_type="group.dot_added",
        source="linq_webhook",
        idempotency_key=f"group.dot_added:{external_event_id}",
        payload={
            "group_title": conversation.title or "group with dot",
            "member_count": len(members),
        },
        delivery_provider="linq",
    )


def _linq_group_chat_id(data: dict[str, object]) -> str | None:
    chat = data.get("chat")
    if isinstance(chat, dict) and isinstance(chat.get("id"), str):
        return str(chat["id"])
    for key in ("chat_id", "id"):
        value = data.get(key)
        if isinstance(value, str):
            return value
    return None


def _participant_is_dot(data: dict[str, object], *, settings: Settings) -> bool:
    participant = data.get("participant")
    details = participant if isinstance(participant, dict) else {}
    if details.get("is_me") is True:
        return True
    handle = details.get("handle") or data.get("handle")
    if not isinstance(handle, str):
        return False
    try:
        return normalize_phone_number(handle) == normalize_phone_number(settings.linq_phone_number)
    except ValueError:
        return False


def _linq_chat_has_active_dot(chat_data: dict[str, object] | None) -> bool:
    if chat_data is None:
        return True
    handles = chat_data.get("handles")
    if not isinstance(handles, list):
        return True
    own_handles = [item for item in handles if isinstance(item, dict) and item.get("is_me") is True]
    if not own_handles:
        return True
    return any(item.get("status") not in {"left", "removed"} for item in own_handles)


async def _load_group_target(
    session: AsyncSession,
    *,
    data: dict[str, object],
) -> tuple[Conversation, ConversationChannel] | None:
    chat_id = _linq_group_chat_id(data)
    if chat_id is None:
        return None
    channel = await session.scalar(
        select(ConversationChannel).where(
            ConversationChannel.provider == PROVIDER,
            ConversationChannel.external_id == chat_id,
        )
    )
    if channel is None:
        return None
    conversation = await session.get(Conversation, channel.conversation_id)
    if conversation is None or conversation.kind != "group":
        return None
    return conversation, channel


async def _resolve_group_from_linq_event(
    session: AsyncSession,
    *,
    linq_client: LinqClient | None,
    data: dict[str, object],
) -> tuple[Conversation, ConversationChannel] | None:
    existing = await _load_group_target(session, data=data)
    if existing is not None:
        return existing
    chat_id = _linq_group_chat_id(data)
    if chat_id is None:
        return None
    chat_data = await _load_linq_chat_data(linq_client, chat_id)
    if chat_data is None or chat_data.get("is_group") is False:
        return None
    handles = chat_data.get("handles")
    if not isinstance(handles, list):
        return None
    sender_handle = next(
        (
            item.get("handle")
            for item in handles
            if isinstance(item, dict)
            and item.get("is_me") is not True
            and isinstance(item.get("handle"), str)
            and str(item["handle"]).startswith("+")
        ),
        None,
    )
    if not isinstance(sender_handle, str):
        return None
    sender = (await resolve_user_from_phone(session, sender_handle)).user
    conversation, channel, _ = await resolve_linq_group_conversation(
        session,
        external_chat_id=chat_id,
        sender=sender,
        service=str(chat_data.get("service")) if chat_data.get("service") else None,
        chat_data=chat_data,
        claim_owner=False,
    )
    return conversation, channel


async def _handle_linq_chat_created(
    session: AsyncSession,
    data: dict[str, object],
) -> tuple[Conversation, ConversationChannel] | None:
    if data.get("is_group") is not True:
        return None
    chat_id = data.get("id")
    handles = data.get("handles")
    if not isinstance(chat_id, str) or not isinstance(handles, list):
        return None
    dot_handle = next(
        (
            item
            for item in handles
            if isinstance(item, dict)
            and item.get("is_me") is True
            and item.get("status") != "removed"
        ),
        None,
    )
    if dot_handle is None:
        return None
    sender_handle = next(
        (
            item.get("handle")
            for item in handles
            if isinstance(item, dict)
            and item.get("is_me") is not True
            and isinstance(item.get("handle"), str)
            and str(item["handle"]).startswith("+")
        ),
        None,
    )
    if not isinstance(sender_handle, str):
        return None
    sender = (await resolve_user_from_phone(session, sender_handle)).user
    conversation, channel, _ = await resolve_linq_group_conversation(
        session,
        external_chat_id=chat_id,
        sender=sender,
        service=str(data.get("service")) if data.get("service") else None,
        chat_data=data,
        claim_owner=False,
    )
    conversation.status = "active"
    channel.status = "active"
    return conversation, channel


async def _is_reply_to_benji(
    session: AsyncSession,
    *,
    conversation_id: UUID,
    external_message_id: str | None,
) -> bool:
    if external_message_id is None:
        return False
    return bool(
        await session.scalar(
            select(MessageDelivery.id)
            .join(Message, Message.id == MessageDelivery.message_id)
            .where(
                MessageDelivery.provider == PROVIDER,
                MessageDelivery.external_id == external_message_id,
                Message.conversation_id == conversation_id,
                Message.direction == MessageDirection.OUTBOUND.value,
            )
        )
    )
