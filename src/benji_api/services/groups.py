import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from benji_api.models.channel import (
    Conversation,
    ConversationChannel,
    ConversationInvite,
    ConversationKind,
    ConversationMember,
    ConversationMemberRole,
    ConversationMemberStatus,
    GroupResponseMode,
)
from benji_api.models.user import User
from benji_api.services.users import (
    find_user_by_identifier,
    get_primary_user_handle,
    normalize_user_identifier,
)

WEB_PROVIDER = "web"
GROUP_MENTION_PATTERN = re.compile(r"(?:^|\W)@?(?:dot|benji)(?:$|\W)", re.IGNORECASE)


class GroupNotFoundError(LookupError):
    pass


class GroupPermissionError(PermissionError):
    pass


class GroupInviteError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class GroupInviteLink:
    invite: ConversationInvite
    token: str


async def create_web_group(
    session: AsyncSession,
    *,
    owner: User,
    title: str,
) -> Conversation:
    clean_title = title.strip()
    if not 1 <= len(clean_title) <= 120:
        raise ValueError("Group name must contain between 1 and 120 characters")
    conversation = Conversation(
        user_id=owner.id,
        kind=ConversationKind.GROUP.value,
        title=clean_title,
        response_mode=GroupResponseMode.AUTO.value,
        group_owner_source="explicit",
    )
    session.add(conversation)
    await session.flush()
    session.add(
        ConversationMember(
            conversation_id=conversation.id,
            user_id=owner.id,
            external_handle=await get_primary_user_handle(session, owner),
            display_name=owner.display_name,
            role=ConversationMemberRole.OWNER.value,
            status=ConversationMemberStatus.ACTIVE.value,
            service="web",
        )
    )
    session.add(
        ConversationChannel(
            conversation_id=conversation.id,
            provider=WEB_PROVIDER,
            external_id=f"group:{conversation.id}",
            service="web",
        )
    )
    await session.commit()
    return conversation


async def get_conversation_for_member(
    session: AsyncSession,
    *,
    conversation_id: UUID,
    user_id: UUID,
) -> Conversation:
    row = await session.execute(
        select(Conversation, ConversationMember)
        .join(
            ConversationMember,
            ConversationMember.conversation_id == Conversation.id,
        )
        .where(
            Conversation.id == conversation_id,
            ConversationMember.user_id == user_id,
            ConversationMember.status == ConversationMemberStatus.ACTIVE.value,
            Conversation.status == "active",
        )
    )
    result = row.first()
    if result is None:
        raise GroupNotFoundError("Conversation was not found")
    return result[0]


async def list_user_conversations(
    session: AsyncSession,
    *,
    user_id: UUID,
) -> tuple[Conversation, ...]:
    conversations = (
        await session.scalars(
            select(Conversation)
            .join(
                ConversationMember,
                ConversationMember.conversation_id == Conversation.id,
            )
            .where(
                ConversationMember.user_id == user_id,
                ConversationMember.status == ConversationMemberStatus.ACTIVE.value,
                Conversation.status == "active",
            )
            .order_by(Conversation.updated_at.desc())
        )
    ).all()
    return tuple(conversations)


async def list_conversation_members(
    session: AsyncSession,
    *,
    conversation_id: UUID,
    active_only: bool = True,
) -> tuple[tuple[ConversationMember, User | None], ...]:
    statement = (
        select(ConversationMember, User)
        .outerjoin(User, User.id == ConversationMember.user_id)
        .where(ConversationMember.conversation_id == conversation_id)
        .order_by(ConversationMember.joined_at, ConversationMember.created_at)
    )
    if active_only:
        statement = statement.where(
            ConversationMember.status == ConversationMemberStatus.ACTIVE.value
        )
    return tuple((await session.execute(statement)).all())


async def create_group_invite(
    session: AsyncSession,
    *,
    conversation_id: UUID,
    user_id: UUID,
    ttl_hours: int = 168,
    max_uses: int = 20,
) -> GroupInviteLink:
    conversation = await get_conversation_for_member(
        session, conversation_id=conversation_id, user_id=user_id
    )
    if conversation.kind != ConversationKind.GROUP.value:
        raise GroupNotFoundError("Group conversation was not found")
    if conversation.user_id != user_id:
        raise GroupPermissionError("Only the group owner can create invite links")
    token = secrets.token_urlsafe(32)
    invite = ConversationInvite(
        conversation_id=conversation.id,
        created_by_user_id=user_id,
        token_hash=_hash_token(token),
        max_uses=max(1, min(max_uses, 100)),
        expires_at=datetime.now(UTC) + timedelta(hours=max(1, min(ttl_hours, 720))),
    )
    session.add(invite)
    await session.commit()
    return GroupInviteLink(invite=invite, token=token)


async def join_group_from_invite(
    session: AsyncSession,
    *,
    token: str,
    user: User,
) -> Conversation:
    invite = await session.scalar(
        select(ConversationInvite).where(ConversationInvite.token_hash == _hash_token(token))
    )
    now = datetime.now(UTC)
    if (
        invite is None
        or invite.revoked_at is not None
        or _as_utc(invite.expires_at) <= now
        or invite.use_count >= invite.max_uses
    ):
        raise GroupInviteError("This group invite is invalid or has expired")
    conversation = await session.get(Conversation, invite.conversation_id)
    if conversation is None or conversation.kind != ConversationKind.GROUP.value:
        raise GroupInviteError("This group no longer exists")
    member_handle = await get_primary_user_handle(session, user)
    member = await session.scalar(
        select(ConversationMember).where(
            ConversationMember.conversation_id == conversation.id,
            or_(
                ConversationMember.user_id == user.id,
                ConversationMember.external_handle == member_handle,
            ),
        )
    )
    if member is None:
        member = ConversationMember(
            conversation_id=conversation.id,
            user_id=user.id,
            external_handle=member_handle,
            display_name=user.display_name,
            role=ConversationMemberRole.MEMBER.value,
            status=ConversationMemberStatus.ACTIVE.value,
            service="web",
        )
        session.add(member)
        invite.use_count += 1
    else:
        member.user_id = user.id
        member.display_name = user.display_name or member.display_name
        member.status = ConversationMemberStatus.ACTIVE.value
        member.left_at = None
    conversation.updated_at = now
    await session.commit()
    return conversation


async def leave_web_group(
    session: AsyncSession,
    *,
    conversation_id: UUID,
    user_id: UUID,
) -> None:
    conversation = await get_conversation_for_member(
        session, conversation_id=conversation_id, user_id=user_id
    )
    if conversation.kind != ConversationKind.GROUP.value:
        raise GroupNotFoundError("Group conversation was not found")
    member = await session.scalar(
        select(ConversationMember).where(
            ConversationMember.conversation_id == conversation_id,
            ConversationMember.user_id == user_id,
        )
    )
    if member is None:
        raise GroupNotFoundError("Group membership was not found")
    if member.role == ConversationMemberRole.OWNER.value:
        raise GroupPermissionError("The group owner cannot leave yet")
    member.status = ConversationMemberStatus.LEFT.value
    member.left_at = datetime.now(UTC)
    await session.commit()


async def rename_group(
    session: AsyncSession,
    *,
    conversation_id: UUID,
    user_id: UUID,
    title: str,
) -> Conversation:
    conversation = await get_conversation_for_member(
        session, conversation_id=conversation_id, user_id=user_id
    )
    if conversation.kind != ConversationKind.GROUP.value:
        raise GroupNotFoundError("Group conversation was not found")
    if conversation.user_id != user_id:
        raise GroupPermissionError("Only the group owner can rename this group")
    clean_title = title.strip()
    if not 1 <= len(clean_title) <= 120:
        raise ValueError("Group name must contain between 1 and 120 characters")
    conversation.title = clean_title
    await session.commit()
    return conversation


async def resolve_linq_group_conversation(
    session: AsyncSession,
    *,
    external_chat_id: str,
    sender: User,
    service: str | None,
    sender_handle: str | None = None,
    chat_data: dict[str, Any] | None = None,
    claim_owner: bool = False,
) -> tuple[Conversation, ConversationChannel, bool]:
    channel = await session.scalar(
        select(ConversationChannel).where(
            ConversationChannel.provider == "linq",
            ConversationChannel.external_id == external_chat_id,
        )
    )
    created = False
    if channel is None:
        title = _clean_optional_text((chat_data or {}).get("display_name"), 120)
        conversation = Conversation(
            user_id=sender.id,
            kind=ConversationKind.GROUP.value,
            title=title or "group with dot",
            response_mode=GroupResponseMode.AUTO.value,
            group_owner_source="unclaimed",
        )
        session.add(conversation)
        await session.flush()
        channel = ConversationChannel(
            conversation_id=conversation.id,
            provider="linq",
            external_id=external_chat_id,
            service=service,
        )
        session.add(channel)
        created = True
    else:
        conversation = await session.get(Conversation, channel.conversation_id)
        if conversation is None or conversation.kind != ConversationKind.GROUP.value:
            raise GroupPermissionError("Linq chat is not attached to a group conversation")
        if service:
            channel.service = service
    normalized_sender_handle = _canonical_handle(
        sender_handle or await get_primary_user_handle(session, sender),
    )
    sender_member = await _upsert_group_member(
        session,
        conversation=conversation,
        handle=normalized_sender_handle,
        user=sender,
        role=ConversationMemberRole.MEMBER.value,
        service=service,
        external_id=None,
        display_name=sender.display_name,
        status=ConversationMemberStatus.ACTIVE.value,
        joined_at=None,
        left_at=None,
    )
    if chat_data:
        await sync_linq_group_participants(
            session,
            conversation=conversation,
            chat_data=chat_data,
        )
        title = _clean_optional_text(chat_data.get("display_name"), 120)
        if title:
            conversation.title = title
    if claim_owner:
        await claim_group_owner(
            session,
            conversation=conversation,
            member=sender_member,
            source="first_invoker",
        )
    await session.flush()
    return conversation, channel, created


async def sync_linq_group_participants(
    session: AsyncSession,
    *,
    conversation: Conversation,
    chat_data: dict[str, Any],
) -> None:
    handles = chat_data.get("handles")
    if not isinstance(handles, list):
        return
    for raw in handles:
        if not isinstance(raw, dict) or raw.get("is_me") is True:
            continue
        handle = raw.get("handle")
        if not isinstance(handle, str) or not handle.strip():
            continue
        normalized_handle = _canonical_handle(handle)
        user = await _find_optional_identifier_user(session, normalized_handle)
        await _upsert_group_member(
            session,
            conversation=conversation,
            handle=normalized_handle,
            user=user,
            role=ConversationMemberRole.MEMBER.value,
            service=_clean_optional_text(raw.get("service"), 32),
            external_id=_clean_optional_text(raw.get("id"), 255),
            display_name=(
                user.display_name
                if user is not None and user.display_name
                else _clean_optional_text(raw.get("display_name") or raw.get("name"), 120)
            ),
            status=str(raw.get("status") or ConversationMemberStatus.ACTIVE.value),
            joined_at=_parse_datetime(raw.get("joined_at")),
            left_at=_parse_datetime(raw.get("left_at")),
        )


async def apply_linq_group_event(
    session: AsyncSession,
    *,
    event_type: str,
    data: dict[str, Any],
) -> bool:
    chat = data.get("chat")
    chat_id = (
        chat.get("id")
        if isinstance(chat, dict) and isinstance(chat.get("id"), str)
        else data.get("chat_id") or data.get("id")
    )
    if not isinstance(chat_id, str):
        return False
    channel = await session.scalar(
        select(ConversationChannel).where(
            ConversationChannel.provider == "linq",
            ConversationChannel.external_id == chat_id,
        )
    )
    if channel is None:
        return False
    conversation = await session.get(Conversation, channel.conversation_id)
    if conversation is None or conversation.kind != ConversationKind.GROUP.value:
        return False

    if event_type in {"chat.group_name_updated", "chat.group_icon_updated"}:
        if event_type == "chat.group_name_updated":
            title = _clean_optional_text(
                data.get("new_value")
                or data.get("display_name")
                or data.get("group_name")
                or data.get("name"),
                120,
            )
            conversation.title = title or "group with dot"
        else:
            conversation.avatar_url = _clean_optional_text(
                data.get("new_value")
                or data.get("group_chat_icon")
                or data.get("icon_url")
                or data.get("url"),
                2_048,
            )
        return True

    if event_type not in {"participant.added", "participant.removed"}:
        return False
    participant = data.get("participant")
    details = participant if isinstance(participant, dict) else {}
    if details.get("is_me") is True:
        if event_type == "participant.removed":
            conversation.status = "inactive"
            channel.status = "inactive"
        else:
            conversation.status = "active"
            channel.status = "active"
        return True
    handle = details.get("handle") or data.get("handle")
    if not isinstance(handle, str):
        return False
    normalized_handle = _canonical_handle(handle)
    user = await _find_optional_identifier_user(session, normalized_handle)
    status = (
        ConversationMemberStatus.ACTIVE.value
        if event_type == "participant.added"
        else str(details.get("status") or ConversationMemberStatus.REMOVED.value)
    )
    member = await _upsert_group_member(
        session,
        conversation=conversation,
        handle=normalized_handle,
        user=user,
        role=ConversationMemberRole.MEMBER.value,
        service=_clean_optional_text(details.get("service"), 32),
        external_id=_clean_optional_text(details.get("id"), 255),
        display_name=(
            user.display_name
            if user is not None and user.display_name
            else _clean_optional_text(details.get("display_name") or details.get("name"), 120)
        ),
        status=status,
        joined_at=_parse_datetime(details.get("joined_at") or data.get("added_at")),
        left_at=_parse_datetime(details.get("left_at") or data.get("removed_at")),
    )
    if event_type == "participant.removed" and member.role == ConversationMemberRole.OWNER.value:
        replacement = await session.scalar(
            select(ConversationMember)
            .where(
                ConversationMember.conversation_id == conversation.id,
                ConversationMember.status == ConversationMemberStatus.ACTIVE.value,
                ConversationMember.id != member.id,
            )
            .order_by(ConversationMember.joined_at, ConversationMember.created_at)
        )
        if replacement is not None:
            member.role = ConversationMemberRole.MEMBER.value
            replacement.role = ConversationMemberRole.OWNER.value
            conversation.group_owner_source = "transferred"
            if replacement.user_id is not None:
                conversation.user_id = replacement.user_id
        else:
            conversation.group_owner_source = "unclaimed"
    return True


def group_message_addresses_benji(text: str) -> bool:
    return bool(GROUP_MENTION_PATTERN.search(text))


def member_label(member: ConversationMember, user: User | None) -> str:
    if user is not None and user.display_name:
        return user.display_name
    if member.display_name:
        return member.display_name
    return "an unnamed group member"


async def claim_group_owner(
    session: AsyncSession,
    *,
    conversation: Conversation,
    member: ConversationMember,
    source: str = "first_invoker",
) -> bool:
    if conversation.group_owner_source not in {None, "unclaimed", "legacy_inferred"}:
        return False
    current_owners = (
        await session.scalars(
            select(ConversationMember).where(
                ConversationMember.conversation_id == conversation.id,
                ConversationMember.role == ConversationMemberRole.OWNER.value,
            )
        )
    ).all()
    for current in current_owners:
        current.role = ConversationMemberRole.MEMBER.value
    member.role = ConversationMemberRole.OWNER.value
    conversation.group_owner_source = source
    if member.user_id is not None:
        conversation.user_id = member.user_id
    return True


def group_owner_context(
    members: tuple[tuple[ConversationMember, User | None], ...],
    *,
    source: str | None,
) -> tuple[str, str]:
    owner = next(
        (
            member_label(member, user)
            for member, user in members
            if member.role == ConversationMemberRole.OWNER.value
        ),
        "not established yet",
    )
    basis = {
        "explicit": "created the web group",
        "first_invoker": "was the first member to invoke dot after it joined",
        "transferred": "inherited ownership after the previous owner left",
    }.get(source, "has not been reliably identified by the channel")
    return owner, basis


def group_app_participant_names(
    members: tuple[tuple[ConversationMember, User | None], ...],
) -> list[str]:
    names: list[str] = []
    used: set[str] = set()
    for ordinal, (member, user) in enumerate(members, start=1):
        known_name = (
            user.display_name if user is not None and user.display_name else member.display_name
        )
        base_name = known_name or f"person {ordinal}"
        name = base_name
        suffix = 2
        while name.casefold() in used:
            name = f"{base_name} {suffix}"
            suffix += 1
        names.append(name)
        used.add(name.casefold())
    return names


async def _upsert_group_member(
    session: AsyncSession,
    *,
    conversation: Conversation,
    handle: str,
    user: User | None,
    role: str,
    service: str | None,
    external_id: str | None,
    display_name: str | None,
    status: str,
    joined_at: datetime | None,
    left_at: datetime | None,
) -> ConversationMember:
    member_filters = [ConversationMember.external_handle == handle]
    if user is not None:
        member_filters.append(ConversationMember.user_id == user.id)
    member = await session.scalar(
        select(ConversationMember).where(
            ConversationMember.conversation_id == conversation.id,
            or_(*member_filters),
        )
    )
    if member is None:
        member = ConversationMember(
            conversation_id=conversation.id,
            external_handle=handle,
            user_id=user.id if user is not None else None,
            display_name=display_name,
            role=role,
        )
        session.add(member)
    elif user is not None:
        member.user_id = user.id
    member.service = service or member.service
    member.external_id = external_id or member.external_id
    member.display_name = display_name or member.display_name
    member.status = status
    member.joined_at = joined_at or member.joined_at
    member.left_at = left_at
    if role == ConversationMemberRole.OWNER.value:
        member.role = role
    return member


async def _find_optional_identifier_user(
    session: AsyncSession,
    handle: str,
) -> User | None:
    try:
        return await find_user_by_identifier(session, handle)
    except ValueError:
        return None


def _canonical_handle(handle: str) -> str:
    try:
        normalized = normalize_user_identifier(handle)
    except ValueError:
        return handle.strip()
    return normalized.value


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _clean_optional_text(value: object, max_length: int) -> str | None:
    if not isinstance(value, str):
        return None
    clean = value.strip()
    return clean[:max_length] if clean else None
