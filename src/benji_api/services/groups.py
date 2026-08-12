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
from benji_api.models.generated_app import GeneratedApp
from benji_api.models.generated_app_v2 import (
    GeneratedAppAccessTicket,
    GeneratedAppMembership,
    GeneratedAppRole,
    GeneratedAppSession,
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
                ConversationMember.user_id.is_not(None),
                ConversationMember.id != member.id,
            )
            .order_by(ConversationMember.joined_at, ConversationMember.created_at)
        )
        if replacement is not None:
            await transfer_group_ownership(
                session,
                conversation=conversation,
                successor=replacement,
                source="transferred",
                departing_owner_user_id=member.user_id or conversation.user_id,
            )
        else:
            departing_owner_user_id = member.user_id or conversation.user_id
            member.role = ConversationMemberRole.MEMBER.value
            conversation.group_owner_source = "unclaimed"
            await _revoke_unclaimed_group_app_owner(
                session,
                conversation_id=conversation.id,
                departing_owner_user_id=departing_owner_user_id,
            )
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
    if member.user_id is not None:
        await transfer_group_ownership(
            session,
            conversation=conversation,
            successor=member,
            source=source,
        )
        return True
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
    return True


async def transfer_group_ownership(
    session: AsyncSession,
    *,
    conversation: Conversation,
    successor: ConversationMember,
    source: str = "transferred",
    departing_owner_user_id: UUID | None = None,
) -> None:
    """Transfer a group and all of its generated apps in one transaction.

    The caller owns the transaction. Explicit sessions belonging to a departed owner are
    revoked, while anonymous member access for the rest of the group remains usable.
    """
    if conversation.kind != ConversationKind.GROUP.value:
        raise GroupPermissionError("Only group conversations can transfer ownership")
    if successor.user_id is None:
        raise GroupPermissionError("A group owner must have a linked Dot account")

    await session.flush()
    conversation_statement = select(Conversation).where(Conversation.id == conversation.id)
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        conversation_statement = conversation_statement.with_for_update()
    locked_conversation = await session.scalar(conversation_statement)
    if locked_conversation is None:
        raise GroupNotFoundError("Group conversation was not found")

    member_statement = select(ConversationMember).where(
        ConversationMember.conversation_id == locked_conversation.id
    )
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        member_statement = member_statement.with_for_update()
    members = list((await session.scalars(member_statement)).all())
    locked_successor = next((item for item in members if item.id == successor.id), None)
    if (
        locked_successor is None
        or locked_successor.user_id is None
        or locked_successor.status != ConversationMemberStatus.ACTIVE.value
    ):
        raise GroupPermissionError("The new owner must be an active group member")

    new_owner_id = locked_successor.user_id
    prior_owner_ids = {locked_conversation.user_id}
    prior_owner_ids.update(
        item.user_id
        for item in members
        if item.role == ConversationMemberRole.OWNER.value and item.user_id is not None
    )
    for item in members:
        if item.role == ConversationMemberRole.OWNER.value:
            item.role = ConversationMemberRole.MEMBER.value
    locked_successor.role = ConversationMemberRole.OWNER.value
    locked_conversation.user_id = new_owner_id
    locked_conversation.group_owner_source = source

    app_statement = select(GeneratedApp).where(
        GeneratedApp.conversation_id == locked_conversation.id
    )
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        app_statement = app_statement.with_for_update()
    generated_apps = list((await session.scalars(app_statement)).all())
    prior_owner_ids.update(app.user_id for app in generated_apps)
    prior_owner_ids.discard(new_owner_id)

    for generated_app in generated_apps:
        generated_app.user_id = new_owner_id
        await _transfer_generated_app_access(
            session,
            app_id=generated_app.id,
            new_owner_id=new_owner_id,
            prior_owner_ids=prior_owner_ids,
            departing_owner_user_id=departing_owner_user_id,
        )
    await session.flush()


async def _transfer_generated_app_access(
    session: AsyncSession,
    *,
    app_id: UUID,
    new_owner_id: UUID,
    prior_owner_ids: set[UUID],
    departing_owner_user_id: UUID | None,
) -> None:
    memberships = list(
        (
            await session.scalars(
                select(GeneratedAppMembership).where(GeneratedAppMembership.app_id == app_id)
            )
        ).all()
    )
    new_owner_membership = next(
        (item for item in memberships if item.user_id == new_owner_id),
        None,
    )
    if new_owner_membership is None:
        session.add(
            GeneratedAppMembership(
                app_id=app_id,
                user_id=new_owner_id,
                role=GeneratedAppRole.OWNER.value,
            )
        )
    else:
        new_owner_membership.role = GeneratedAppRole.OWNER.value

    for membership in memberships:
        if membership.user_id == new_owner_id:
            continue
        if membership.user_id == departing_owner_user_id:
            await session.delete(membership)
        elif membership.role == GeneratedAppRole.OWNER.value:
            membership.role = GeneratedAppRole.MEMBER.value

    now = datetime.now(UTC)
    sessions = list(
        (
            await session.scalars(
                select(GeneratedAppSession).where(GeneratedAppSession.app_id == app_id)
            )
        ).all()
    )
    for app_session in sessions:
        belongs_to_departed_owner = (
            departing_owner_user_id is not None and app_session.user_id == departing_owner_user_id
        )
        has_stale_owner_access = (
            app_session.role == GeneratedAppRole.OWNER.value and app_session.user_id != new_owner_id
        )
        if belongs_to_departed_owner or (
            departing_owner_user_id is not None and has_stale_owner_access
        ):
            app_session.revoked_at = now
        elif has_stale_owner_access:
            app_session.role = GeneratedAppRole.MEMBER.value

    tickets = list(
        (
            await session.scalars(
                select(GeneratedAppAccessTicket).where(GeneratedAppAccessTicket.app_id == app_id)
            )
        ).all()
    )
    for ticket in tickets:
        belongs_to_departed_owner = (
            departing_owner_user_id is not None
            and ticket.principal_user_id == departing_owner_user_id
        )
        has_stale_owner_access = (
            ticket.role == GeneratedAppRole.OWNER.value and ticket.principal_user_id != new_owner_id
        )
        if belongs_to_departed_owner or (
            departing_owner_user_id is not None and has_stale_owner_access
        ):
            ticket.expires_at = now
        elif has_stale_owner_access:
            ticket.role = GeneratedAppRole.MEMBER.value
        if ticket.issued_by_user_id in prior_owner_ids:
            ticket.issued_by_user_id = new_owner_id


async def _revoke_unclaimed_group_app_owner(
    session: AsyncSession,
    *,
    conversation_id: UUID,
    departing_owner_user_id: UUID,
) -> None:
    """Remove owner authority while a group has no linked successor.

    Anonymous member sessions and tickets remain valid so the shared app keeps working for the
    group. A later linked owner claim reuses the normal atomic transfer path.
    """

    app_statement = select(GeneratedApp).where(GeneratedApp.conversation_id == conversation_id)
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        app_statement = app_statement.with_for_update()
    generated_apps = list((await session.scalars(app_statement)).all())
    now = datetime.now(UTC)

    for generated_app in generated_apps:
        memberships = list(
            (
                await session.scalars(
                    select(GeneratedAppMembership).where(
                        GeneratedAppMembership.app_id == generated_app.id
                    )
                )
            ).all()
        )
        for membership in memberships:
            if (
                membership.user_id == departing_owner_user_id
                or membership.role == GeneratedAppRole.OWNER.value
            ):
                await session.delete(membership)

        app_sessions = list(
            (
                await session.scalars(
                    select(GeneratedAppSession).where(
                        GeneratedAppSession.app_id == generated_app.id
                    )
                )
            ).all()
        )
        for app_session in app_sessions:
            if (
                app_session.user_id == departing_owner_user_id
                or app_session.role == GeneratedAppRole.OWNER.value
            ):
                app_session.revoked_at = now

        tickets = list(
            (
                await session.scalars(
                    select(GeneratedAppAccessTicket).where(
                        GeneratedAppAccessTicket.app_id == generated_app.id
                    )
                )
            ).all()
        )
        for ticket in tickets:
            if (
                ticket.principal_user_id == departing_owner_user_id
                or ticket.role == GeneratedAppRole.OWNER.value
            ):
                ticket.expires_at = now


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
