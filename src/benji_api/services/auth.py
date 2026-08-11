from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from benji_api.integrations.auth import AuthTokenVerifier, VerifiedAuthToken
from benji_api.models.auth import AuthIdentity
from benji_api.models.user import User, UserIdentifierKind
from benji_api.services.users import (
    UserIdentifierConflictError,
    find_user_by_identifier,
    link_user_identifier,
    normalize_user_identifier,
)


class AuthUserNotFoundError(LookupError):
    pass


class AuthIdentityNotFoundError(LookupError):
    pass


class AuthIdentityConflictError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AuthEligibility:
    kind: UserIdentifierKind
    normalized_identifier: str


async def check_auth_eligibility(
    session: AsyncSession,
    *,
    identifier: str,
) -> AuthEligibility:
    normalized = normalize_user_identifier(identifier)
    user = await find_user_by_identifier(
        session,
        normalized.value,
        expected_kind=normalized.kind,
    )
    if user is None:
        raise AuthUserNotFoundError(
            "No Dot account exists for that phone number or email. "
            "Message Dot first to get started."
        )
    return AuthEligibility(
        kind=normalized.kind,
        normalized_identifier=normalized.value,
    )


async def resolve_authenticated_user(
    session: AsyncSession,
    *,
    token: str,
    verifier: AuthTokenVerifier,
) -> User:
    verified = await verifier.verify_token(token)
    identity = await session.scalar(
        select(AuthIdentity).where(
            AuthIdentity.provider == verified.provider,
            AuthIdentity.provider_subject == verified.subject,
        )
    )
    if identity is not None:
        return await _resolve_linked_identity(session, identity=identity, verified=verified)

    user, verified_phone = await _resolve_user_from_verified_claims(session, verified=verified)
    now = datetime.now(UTC)
    identity = AuthIdentity(
        user_id=user.id,
        provider=verified.provider,
        provider_subject=verified.subject,
        verified_phone=verified_phone,
        verified_at=now,
        status="active",
    )
    try:
        async with session.begin_nested():
            session.add(identity)
            await session.flush()
    except IntegrityError:
        concurrent_identity = await session.scalar(
            select(AuthIdentity).where(
                AuthIdentity.provider == verified.provider,
                AuthIdentity.provider_subject == verified.subject,
            )
        )
        if concurrent_identity is None:
            raise
        if concurrent_identity.status != "active" or concurrent_identity.user_id != user.id:
            raise AuthIdentityConflictError(
                "Authenticated identity is already linked to another Dot account"
            ) from None
    await session.commit()
    return user


async def _resolve_linked_identity(
    session: AsyncSession,
    *,
    identity: AuthIdentity,
    verified: VerifiedAuthToken,
) -> User:
    if identity.status != "active":
        raise AuthIdentityNotFoundError("Authenticated identity is not active")
    user = await session.get(User, identity.user_id)
    if user is None:
        raise AuthIdentityNotFoundError("Authenticated Dot user no longer exists")

    verified_phone = _normalized_claim(
        verified.phone_number,
        expected_kind=UserIdentifierKind.PHONE,
    )
    changed = False
    if verified_phone is not None and identity.verified_phone != verified_phone:
        identity.verified_phone = verified_phone
        changed = True
    if identity.verified_at is None:
        identity.verified_at = datetime.now(UTC)
        changed = True
    if changed:
        await session.commit()
    return user


async def _resolve_user_from_verified_claims(
    session: AsyncSession,
    *,
    verified: VerifiedAuthToken,
) -> tuple[User, str | None]:
    claims = (
        (verified.phone_number, UserIdentifierKind.PHONE),
        (verified.email, UserIdentifierKind.EMAIL),
    )
    candidate_users: dict[object, User] = {}
    normalized_claims: list[tuple[str, UserIdentifierKind]] = []
    verified_phone: str | None = None
    for claim, kind in claims:
        normalized = _normalized_claim(claim, expected_kind=kind)
        if normalized is None:
            continue
        normalized_claims.append((normalized, kind))
        if kind == UserIdentifierKind.PHONE:
            verified_phone = normalized
        user = await find_user_by_identifier(
            session,
            normalized,
            expected_kind=kind,
        )
        if user is not None:
            candidate_users[user.id] = user

    if not candidate_users:
        raise AuthUserNotFoundError(
            "No Dot account matches the verified Firebase phone number or email"
        )
    if len(candidate_users) != 1:
        raise AuthIdentityConflictError(
            "Verified Firebase identifiers belong to different Dot accounts"
        )
    user = next(iter(candidate_users.values()))
    verified_at = datetime.now(UTC)
    try:
        for normalized, kind in normalized_claims:
            await link_user_identifier(
                session,
                user=user,
                value=normalized,
                expected_kind=kind,
                source=verified.provider,
                verified_at=verified_at,
            )
    except UserIdentifierConflictError as error:
        raise AuthIdentityConflictError(
            "Verified Firebase identifier belongs to another Dot account"
        ) from error
    return user, verified_phone


def _normalized_claim(
    value: str | None,
    *,
    expected_kind: UserIdentifierKind,
) -> str | None:
    if value is None:
        return None
    return normalize_user_identifier(value, expected_kind=expected_kind).value
