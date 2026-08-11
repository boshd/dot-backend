from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from benji_api.agents.channel_delivery import load_recent_messages
from benji_api.agents.followups import cancel_pending_follow_ups
from benji_api.agents.group_participation import decide_group_participation
from benji_api.agents.onboarding import run_onboarding_turn
from benji_api.agents.prompts.group import build_group_module
from benji_api.agents.service import run_agent_turn
from benji_api.agents.tools import ToolRegistry
from benji_api.agents.types import ModelProvider
from benji_api.config import Settings
from benji_api.memory.types import EmbeddingProvider
from benji_api.models.channel import (
    Conversation,
    ConversationChannel,
    ConversationKind,
    Message,
    MessageDirection,
    MessageStatus,
)
from benji_api.models.user import OnboardingStatus, User
from benji_api.services.channels import resolve_channel_conversation
from benji_api.services.groups import (
    get_conversation_for_member,
    group_message_addresses_benji,
    group_owner_context,
    list_conversation_members,
    member_label,
)

WEB_PROVIDER = "web"


class WebChatConversationNotFoundError(LookupError):
    pass


@dataclass(frozen=True, slots=True)
class WebChatSession:
    user: User
    conversation: Conversation
    messages: tuple[Message, ...]
    user_created: bool


@dataclass(frozen=True, slots=True)
class WebChatTurn:
    user: User
    conversation: Conversation
    assistant_messages: tuple[Message, ...]


def _web_channel_external_id(user_id: UUID) -> str:
    return f"user:{user_id}"


async def open_web_chat_session(
    session: AsyncSession,
    *,
    user: User,
    user_created: bool = False,
    conversation_id: UUID | None = None,
) -> WebChatSession:
    if conversation_id is None:
        channel_resolution = await resolve_channel_conversation(
            session,
            user_id=user.id,
            provider=WEB_PROVIDER,
            external_id=_web_channel_external_id(user.id),
            service="web",
        )
        conversation = channel_resolution.conversation
    else:
        try:
            conversation = await get_conversation_for_member(
                session,
                conversation_id=conversation_id,
                user_id=user.id,
            )
        except LookupError as error:
            raise WebChatConversationNotFoundError("Dot conversation was not found") from error
        channel = await session.scalar(
            select(ConversationChannel).where(
                ConversationChannel.conversation_id == conversation.id,
                ConversationChannel.provider == WEB_PROVIDER,
            )
        )
        if channel is None:
            channel = ConversationChannel(
                conversation_id=conversation.id,
                provider=WEB_PROVIDER,
                external_id=(
                    _web_channel_external_id(user.id)
                    if conversation.kind == ConversationKind.DIRECT.value
                    else f"group:{conversation.id}"
                ),
                service="web",
            )
            session.add(channel)
    await session.commit()

    messages = tuple(
        (
            await session.scalars(
                select(Message)
                .where(
                    Message.conversation_id == conversation.id,
                    Message.content != "",
                )
                .order_by(Message.created_at)
            )
        ).all()
    )
    return WebChatSession(
        user=user,
        conversation=conversation,
        messages=messages,
        user_created=user_created,
    )


async def send_web_chat_message(
    session: AsyncSession,
    *,
    user: User,
    conversation_id: UUID,
    client_message_id: UUID,
    content: str,
    provider: ModelProvider,
    tools: ToolRegistry,
    settings: Settings,
    embedding_provider: EmbeddingProvider | None = None,
) -> WebChatTurn:
    conversation = await session.get(Conversation, conversation_id)
    if conversation is None:
        raise WebChatConversationNotFoundError("Dot conversation was not found")
    if conversation.kind == ConversationKind.DIRECT.value:
        if conversation.user_id != user.id:
            raise WebChatConversationNotFoundError("Dot conversation was not found")
    else:
        try:
            conversation = await get_conversation_for_member(
                session,
                conversation_id=conversation_id,
                user_id=user.id,
            )
        except LookupError as error:
            raise WebChatConversationNotFoundError("Dot conversation was not found") from error

    channel = await session.scalar(
        select(ConversationChannel).where(
            ConversationChannel.conversation_id == conversation.id,
            ConversationChannel.provider == WEB_PROVIDER,
        )
    )
    if channel is None:
        raise WebChatConversationNotFoundError("Web channel was not found")

    idempotency_key = f"benji:web:{client_message_id}:reply"
    existing_reply = await session.scalar(
        select(Message).where(
            Message.source_channel == WEB_PROVIDER,
            Message.idempotency_key == idempotency_key,
        )
    )
    if existing_reply is not None:
        existing_replies = await _load_response_group(session, existing_reply)
        return WebChatTurn(
            user=user,
            conversation=conversation,
            assistant_messages=existing_replies,
        )

    inbound_external_id = str(client_message_id)
    inbound = await session.scalar(
        select(Message).where(
            Message.source_channel == WEB_PROVIDER,
            Message.source_external_id == inbound_external_id,
        )
    )
    if inbound is None:
        inbound = Message(
            conversation_id=conversation.id,
            user_id=user.id,
            sender_user_id=user.id,
            source_binding_id=channel.id,
            source_channel=WEB_PROVIDER,
            source_external_id=inbound_external_id,
            direction=MessageDirection.INBOUND.value,
            status=MessageStatus.RECEIVED.value,
            content=content.strip(),
            raw_payload={"client_message_id": inbound_external_id},
        )
        session.add(inbound)
        conversation.updated_at = datetime.now(UTC)
        await cancel_pending_follow_ups(session, conversation_id=conversation.id)
        await session.commit()

    should_run_group_agent = False
    group_modules = ()
    if conversation.kind == ConversationKind.GROUP.value:
        prior_inbound = await session.scalar(
            select(Message.id).where(
                Message.conversation_id == conversation.id,
                Message.direction == MessageDirection.INBOUND.value,
                Message.id != inbound.id,
            )
        )
        force_group_response = bool(
            prior_inbound is None
            or conversation.response_mode == "always"
            or group_message_addresses_benji(content)
        )
        should_run_group_agent = force_group_response or conversation.response_mode == "auto"
        if should_run_group_agent:
            members = await list_conversation_members(session, conversation_id=conversation.id)
            owner_name, owner_basis = group_owner_context(
                members, source=conversation.group_owner_source
            )
            speaker_member = next(
                (
                    (member, member_user)
                    for member, member_user in members
                    if member_user is not None and member_user.id == user.id
                ),
                None,
            )
            group_modules = (
                build_group_module(
                    title=conversation.title or "group with dot",
                    current_speaker=(
                        member_label(*speaker_member)
                        if speaker_member is not None
                        else user.display_name or "a group member"
                    ),
                    member_names=tuple(
                        member_label(member, member_user) for member, member_user in members
                    ),
                    owner_name=owner_name,
                    owner_basis=owner_basis,
                    channel="a shared web group chat",
                ),
            )
            if not force_group_response:
                messages = await load_recent_messages(
                    session,
                    conversation_id=conversation.id,
                    limit=settings.agent_context_message_limit,
                )
                participation = await decide_group_participation(
                    provider=provider,
                    messages=messages,
                    group_module=group_modules[0],
                    force_response=False,
                )
                should_run_group_agent = participation.should_respond

    if conversation.kind == ConversationKind.GROUP.value and not should_run_group_agent:
        return WebChatTurn(
            user=user,
            conversation=conversation,
            assistant_messages=(),
        )
    if (
        user.onboarding_status == OnboardingStatus.COMPLETE.value
        or conversation.kind == ConversationKind.GROUP.value
    ):
        reply = await run_agent_turn(
            conversation_id=conversation.id,
            user_id=user.id,
            trigger_message_id=inbound.id,
            source_channel=WEB_PROVIDER,
            source_binding_id=channel.id,
            idempotency_key=idempotency_key,
            provider=provider,
            tools=tools,
            settings=settings,
            embedding_provider=embedding_provider,
            state_modules=group_modules,
            allow_follow_up=conversation.kind == ConversationKind.DIRECT.value,
        )
    else:
        reply = await run_onboarding_turn(
            conversation_id=conversation.id,
            user_id=user.id,
            trigger_message_id=inbound.id,
            is_new_user=False,
            source_channel=WEB_PROVIDER,
            source_binding_id=channel.id,
            idempotency_key=idempotency_key,
            provider=provider,
            settings=settings,
        )

    await session.refresh(user)
    assistant_messages = []
    for persisted_reply in reply.replies:
        assistant_message = await session.get(Message, persisted_reply.message_id)
        if assistant_message is None:
            raise RuntimeError("Persisted web reply was not found")
        assistant_messages.append(assistant_message)
    return WebChatTurn(
        user=user,
        conversation=conversation,
        assistant_messages=tuple(assistant_messages),
    )


async def _load_response_group(
    session: AsyncSession,
    first_message: Message,
) -> tuple[Message, ...]:
    if first_message.response_group_id is None:
        return (first_message,)
    messages = (
        await session.scalars(
            select(Message)
            .where(Message.response_group_id == first_message.response_group_id)
            .order_by(Message.response_ordinal)
        )
    ).all()
    return tuple(messages)
