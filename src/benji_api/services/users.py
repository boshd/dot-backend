from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from benji_api.models.user import (
    User,
    UserIdentifier,
    UserIdentifierKind,
    UserIdentifierStatus,
)
from benji_api.schemas.phone import normalize_phone_number


class UserIdentifierConflictError(RuntimeError):
    pass


class UserIdentifierRevokedError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class NormalizedUserIdentifier:
    kind: UserIdentifierKind
    value: str


@dataclass(frozen=True, slots=True)
class UserResolution:
    user: User
    created: bool
    identifier: UserIdentifier


def normalize_email_address(value: str) -> str:
    candidate = value.strip()
    if candidate.count("@") != 1 or len(candidate) > 320:
        raise ValueError("email address must be valid")
    local_part, domain = candidate.rsplit("@", 1)
    if (
        not local_part
        or len(local_part) > 64
        or local_part.startswith(".")
        or local_part.endswith(".")
        or ".." in local_part
        or any(character.isspace() or ord(character) < 32 for character in local_part)
    ):
        raise ValueError("email address must be valid")
    try:
        ascii_domain = domain.rstrip(".").encode("idna").decode("ascii").casefold()
    except UnicodeError as error:
        raise ValueError("email address must be valid") from error
    labels = ascii_domain.split(".")
    if not ascii_domain or any(
        not label
        or len(label) > 63
        or label.startswith("-")
        or label.endswith("-")
        or not all(character.isalnum() or character == "-" for character in label)
        for label in labels
    ):
        raise ValueError("email address must be valid")
    normalized = f"{local_part.casefold()}@{ascii_domain}"
    if len(normalized) > 320:
        raise ValueError("email address must be valid")
    return normalized


def normalize_user_identifier(
    value: str,
    expected_kind: UserIdentifierKind | str | None = None,
) -> NormalizedUserIdentifier:
    try:
        kind = UserIdentifierKind(expected_kind) if expected_kind is not None else None
    except ValueError as error:
        raise ValueError("identifier kind must be phone or email") from error
    if kind == UserIdentifierKind.EMAIL or (kind is None and "@" in value):
        return NormalizedUserIdentifier(
            kind=UserIdentifierKind.EMAIL,
            value=normalize_email_address(value),
        )
    return NormalizedUserIdentifier(
        kind=UserIdentifierKind.PHONE,
        value=normalize_phone_number(value),
    )


async def find_user_by_identifier(
    session: AsyncSession,
    value: str,
    expected_kind: UserIdentifierKind | str | None = None,
) -> User | None:
    normalized = normalize_user_identifier(value, expected_kind)
    identifier = await session.scalar(
        select(UserIdentifier).where(
            UserIdentifier.kind == normalized.kind.value,
            UserIdentifier.normalized_value == normalized.value,
        )
    )
    if identifier is not None:
        if identifier.status != UserIdentifierStatus.ACTIVE.value:
            return None
        return await session.get(User, identifier.user_id)

    # This fallback lets manually-created and pre-migration phone users be adopted lazily.
    if normalized.kind != UserIdentifierKind.PHONE:
        return None
    return await session.scalar(select(User).where(User.phone_number == normalized.value))


async def resolve_user_from_identifier(
    session: AsyncSession,
    value: str,
    *,
    expected_kind: UserIdentifierKind | str | None = None,
    source: str = "channel",
    verified: bool = True,
) -> UserResolution:
    """Resolve a canonical user from a phone/email handle, creating it exactly once."""
    normalized = normalize_user_identifier(value, expected_kind)
    now = datetime.now(UTC)
    identifier = await session.scalar(
        select(UserIdentifier).where(
            UserIdentifier.kind == normalized.kind.value,
            UserIdentifier.normalized_value == normalized.value,
        )
    )
    if identifier is not None:
        if identifier.status == UserIdentifierStatus.REVOKED.value:
            raise UserIdentifierRevokedError(
                "Identifier was revoked and requires an explicit recovery flow"
            )
        user = await session.get(User, identifier.user_id)
        if user is None:
            raise RuntimeError("User identifier points to a missing user")
        identifier.verified_at = now if verified else identifier.verified_at
        user.last_seen_at = now
        _sync_legacy_phone(user, identifier)
        return UserResolution(user=user, created=False, identifier=identifier)

    if normalized.kind == UserIdentifierKind.PHONE:
        legacy_user = await session.scalar(
            select(User).where(User.phone_number == normalized.value)
        )
        if legacy_user is not None:
            identifier = await link_user_identifier(
                session,
                user=legacy_user,
                value=normalized.value,
                expected_kind=normalized.kind,
                source="legacy_phone",
                verified_at=(now if verified else legacy_user.phone_verified_at),
                is_primary=True,
            )
            legacy_user.last_seen_at = now
            return UserResolution(user=legacy_user, created=False, identifier=identifier)

    user = User(
        id=uuid4(),
        phone_number=(normalized.value if normalized.kind == UserIdentifierKind.PHONE else None),
        phone_verified_at=(
            now if verified and normalized.kind == UserIdentifierKind.PHONE else None
        ),
        first_seen_at=now,
        last_seen_at=now,
    )
    identifier = UserIdentifier(
        user_id=user.id,
        kind=normalized.kind.value,
        normalized_value=normalized.value,
        display_value=normalized.value,
        source=_clean_source(source),
        verified_at=now if verified else None,
        is_primary=True,
        status=UserIdentifierStatus.ACTIVE.value,
    )

    try:
        async with session.begin_nested():
            session.add(user)
            session.add(identifier)
            await session.flush()
    except IntegrityError as error:
        # Concurrent inbound messages can race on the globally unique identifier.
        identifier = await session.scalar(
            select(UserIdentifier).where(
                UserIdentifier.kind == normalized.kind.value,
                UserIdentifier.normalized_value == normalized.value,
            )
        )
        if identifier is None:
            raise
        user = await session.get(User, identifier.user_id)
        if user is None:
            raise RuntimeError("User identifier points to a missing user") from error
        user.last_seen_at = now
        return UserResolution(user=user, created=False, identifier=identifier)

    return UserResolution(user=user, created=True, identifier=identifier)


async def link_user_identifier(
    session: AsyncSession,
    *,
    user: User,
    value: str,
    expected_kind: UserIdentifierKind | str | None = None,
    source: str,
    verified_at: datetime | None,
    is_primary: bool = False,
) -> UserIdentifier:
    """Attach a verified identifier without ever silently merging two users."""
    normalized = normalize_user_identifier(value, expected_kind)
    existing = await session.scalar(
        select(UserIdentifier).where(
            UserIdentifier.kind == normalized.kind.value,
            UserIdentifier.normalized_value == normalized.value,
        )
    )
    if existing is not None:
        if existing.user_id != user.id:
            raise UserIdentifierConflictError("Identifier belongs to another user")
        existing.status = UserIdentifierStatus.ACTIVE.value
        existing.source = _clean_source(source)
        existing.verified_at = verified_at or existing.verified_at
        if is_primary:
            await _make_primary(session, user_id=user.id, identifier_id=existing.id)
            existing.is_primary = True
        _sync_legacy_phone(user, existing)
        return existing

    has_identifier = await session.scalar(
        select(UserIdentifier.id).where(
            UserIdentifier.user_id == user.id,
            UserIdentifier.status == UserIdentifierStatus.ACTIVE.value,
        )
    )
    primary = is_primary or has_identifier is None
    identifier = UserIdentifier(
        user_id=user.id,
        kind=normalized.kind.value,
        normalized_value=normalized.value,
        display_value=normalized.value,
        source=_clean_source(source),
        verified_at=verified_at,
        is_primary=primary,
        status=UserIdentifierStatus.ACTIVE.value,
    )
    try:
        async with session.begin_nested():
            session.add(identifier)
            await session.flush()
    except IntegrityError as error:
        owner_id = await session.scalar(
            select(UserIdentifier.user_id).where(
                UserIdentifier.kind == normalized.kind.value,
                UserIdentifier.normalized_value == normalized.value,
            )
        )
        if owner_id != user.id:
            raise UserIdentifierConflictError("Identifier belongs to another user") from error
        existing = await session.scalar(
            select(UserIdentifier).where(
                UserIdentifier.kind == normalized.kind.value,
                UserIdentifier.normalized_value == normalized.value,
            )
        )
        if existing is None:
            raise
        return existing
    if primary:
        await _make_primary(session, user_id=user.id, identifier_id=identifier.id)
        identifier.is_primary = True
    _sync_legacy_phone(user, identifier)
    return identifier


async def get_primary_user_handle(session: AsyncSession, user: User) -> str:
    identifier = await session.scalar(
        select(UserIdentifier)
        .where(
            UserIdentifier.user_id == user.id,
            UserIdentifier.status == UserIdentifierStatus.ACTIVE.value,
        )
        .order_by(UserIdentifier.is_primary.desc(), UserIdentifier.created_at, UserIdentifier.id)
        .limit(1)
    )
    if identifier is not None:
        return identifier.normalized_value
    if user.phone_number:
        return user.phone_number
    return f"user:{user.id}"


async def resolve_user_from_phone(
    session: AsyncSession,
    phone_number: str,
) -> UserResolution:
    """Backward-compatible phone-only adapter around canonical identity resolution."""
    return await resolve_user_from_identifier(
        session,
        phone_number,
        expected_kind=UserIdentifierKind.PHONE,
        source="phone",
    )


async def _make_primary(
    session: AsyncSession,
    *,
    user_id: UUID,
    identifier_id: UUID | None = None,
) -> None:
    statement = update(UserIdentifier).where(UserIdentifier.user_id == user_id)
    if identifier_id is not None:
        statement = statement.where(UserIdentifier.id != identifier_id)
    await session.execute(statement.values(is_primary=False))


def _sync_legacy_phone(user: User, identifier: UserIdentifier) -> None:
    if identifier.kind != UserIdentifierKind.PHONE.value:
        return
    if user.phone_number is None or identifier.is_primary:
        user.phone_number = identifier.normalized_value
        user.phone_verified_at = identifier.verified_at


def _clean_source(source: str) -> str:
    clean = source.strip()
    if not clean:
        raise ValueError("identifier source must not be empty")
    return clean[:64]
