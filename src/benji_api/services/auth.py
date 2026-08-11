from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from benji_api.config import Settings
from benji_api.integrations.stytch.client import StytchAuthClient, StytchProviderError
from benji_api.models.auth import AuthIdentity
from benji_api.models.user import User
from benji_api.schemas.phone import normalize_phone_number

STYTCH_PROVIDER = "stytch"


class AuthUserNotFoundError(LookupError):
    pass


class AuthIdentityNotFoundError(LookupError):
    pass


@dataclass(frozen=True, slots=True)
class PhoneAuthChallenge:
    method_id: str
    expires_in_seconds: int


async def start_phone_authentication(
    session: AsyncSession,
    *,
    phone_number: str,
    client: StytchAuthClient,
    settings: Settings,
) -> PhoneAuthChallenge:
    normalized_phone = normalize_phone_number(phone_number)
    user = await session.scalar(select(User).where(User.phone_number == normalized_phone))
    if user is None:
        raise AuthUserNotFoundError(
            "No Dot account exists for that number. Message Dot first to get started."
        )

    identity = await session.scalar(
        select(AuthIdentity).where(
            AuthIdentity.user_id == user.id,
            AuthIdentity.provider == STYTCH_PROVIDER,
        )
    )
    if identity is None:
        challenge = await client.login_or_create_sms_otp(
            phone_number=user.phone_number,
            expiration_minutes=settings.stytch_otp_expiration_minutes,
        )
        identity = AuthIdentity(
            user_id=user.id,
            provider=STYTCH_PROVIDER,
            provider_subject=challenge.provider_user_id,
        )
        session.add(identity)
        await session.commit()
    else:
        challenge = await client.send_sms_otp(
            provider_user_id=identity.provider_subject,
            phone_number=user.phone_number,
            expiration_minutes=settings.stytch_otp_expiration_minutes,
        )
        if challenge.provider_user_id != identity.provider_subject:
            raise StytchProviderError("Stytch returned an unexpected user identity")
    return PhoneAuthChallenge(
        method_id=challenge.method_id,
        expires_in_seconds=settings.stytch_otp_expiration_minutes * 60,
    )


async def resolve_authenticated_user(
    session: AsyncSession,
    *,
    session_jwt: str,
    client: StytchAuthClient,
    settings: Settings,
) -> User:
    provider_subject = await client.authenticate_session_jwt(
        session_jwt=session_jwt,
        max_token_age_seconds=settings.stytch_session_max_token_age_seconds,
    )
    identity = await session.scalar(
        select(AuthIdentity).where(
            AuthIdentity.provider == STYTCH_PROVIDER,
            AuthIdentity.provider_subject == provider_subject,
            AuthIdentity.status == "active",
        )
    )
    if identity is None:
        raise AuthIdentityNotFoundError("Authenticated identity is not linked to Dot")

    user = await session.get(User, identity.user_id)
    if user is None:
        raise AuthIdentityNotFoundError("Authenticated Dot user no longer exists")

    if identity.verified_at is None:
        identity.verified_phone = user.phone_number
        identity.verified_at = datetime.now(UTC)
        await session.commit()
    return user
