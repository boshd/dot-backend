from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from benji_api.config import Settings, get_settings
from benji_api.db.session import get_session
from benji_api.integrations.stytch.client import StytchAuthClient, StytchProviderError
from benji_api.integrations.stytch.dependencies import get_stytch_client
from benji_api.models.user import User
from benji_api.services.auth import AuthIdentityNotFoundError, resolve_authenticated_user
from benji_api.services.users import resolve_user_from_phone


def _session_jwt(request: Request) -> str | None:
    authorization = request.headers.get("authorization")
    if authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() == "bearer" and token:
            return token
    return request.cookies.get("stytch_session_jwt")


async def get_optional_authenticated_user(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    stytch_client: Annotated[StytchAuthClient | None, Depends(get_stytch_client)],
) -> User | None:
    session_jwt = _session_jwt(request)
    if session_jwt is None:
        return None
    if stytch_client is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Stytch authentication is not configured",
        )
    try:
        return await resolve_authenticated_user(
            session,
            session_jwt=session_jwt,
            client=stytch_client,
            settings=settings,
        )
    except (AuthIdentityNotFoundError, StytchProviderError) as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="The authentication session is invalid or expired",
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
