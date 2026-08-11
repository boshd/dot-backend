from functools import lru_cache
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from benji_api.config import Settings, get_settings
from benji_api.db.session import get_session
from benji_api.integrations.auth import AuthProviderError, AuthTokenVerifier
from benji_api.integrations.firebase.dependencies import get_auth_token_verifier
from benji_api.models.user import User
from benji_api.services.auth import (
    AuthIdentityConflictError,
    AuthIdentityNotFoundError,
    AuthUserNotFoundError,
    resolve_authenticated_user,
)
from benji_api.services.auth_rate_limit import AuthEligibilityRateLimiter
from benji_api.services.users import resolve_user_from_phone


@lru_cache
def _auth_eligibility_rate_limiter(
    ip_per_minute: int,
    ip_per_hour: int,
    identifier_per_hour: int,
) -> AuthEligibilityRateLimiter:
    return AuthEligibilityRateLimiter(
        ip_per_minute=ip_per_minute,
        ip_per_hour=ip_per_hour,
        identifier_per_hour=identifier_per_hour,
    )


def get_auth_eligibility_rate_limiter(
    settings: Annotated[Settings, Depends(get_settings)],
) -> AuthEligibilityRateLimiter:
    return _auth_eligibility_rate_limiter(
        settings.auth_eligibility_ip_limit_per_minute,
        settings.auth_eligibility_ip_limit_per_hour,
        settings.auth_eligibility_identifier_limit_per_hour,
    )


def _bearer_token(request: Request) -> str | None:
    authorization = request.headers.get("authorization")
    if authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.casefold() == "bearer" and token.strip():
            return token.strip()
    return None


async def get_optional_authenticated_user(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    verifier: Annotated[AuthTokenVerifier | None, Depends(get_auth_token_verifier)],
) -> User | None:
    token = _bearer_token(request)
    if token is None:
        return None
    if verifier is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication is not configured",
        )
    try:
        return await resolve_authenticated_user(
            session,
            token=token,
            verifier=verifier,
        )
    except (
        AuthIdentityConflictError,
        AuthIdentityNotFoundError,
        AuthProviderError,
        AuthUserNotFoundError,
        ValueError,
    ) as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="The authentication session is invalid or is not linked to Dot",
        ) from error


async def resolve_client_user(
    session: AsyncSession,
    *,
    authenticated_user: User | None,
    phone_number: str | None,
    settings: Settings,
) -> User:
    """Resolve a verified user, with an explicit local-development fallback."""
    if authenticated_user is not None:
        return authenticated_user
    if not settings.web_chat_dev_identity_enabled or phone_number is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sign in to continue",
        )
    resolution = await resolve_user_from_phone(session, phone_number)
    return resolution.user
