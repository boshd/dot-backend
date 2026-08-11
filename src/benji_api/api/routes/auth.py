from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from benji_api.api.dependencies import get_auth_eligibility_rate_limiter
from benji_api.db.session import get_session
from benji_api.services.auth import AuthUserNotFoundError, check_auth_eligibility
from benji_api.services.auth_rate_limit import (
    AuthEligibilityRateLimiter,
    AuthRateLimitExceeded,
)
from benji_api.services.users import normalize_user_identifier

router = APIRouter(prefix="/auth", tags=["authentication"])


class AuthEligibilityRequest(BaseModel):
    identifier: str = Field(min_length=3, max_length=320)


class AuthEligibilityResponse(BaseModel):
    eligible: Literal[True] = True
    kind: Literal["phone", "email"]
    normalized_identifier: str


@router.post("/eligibility", response_model=AuthEligibilityResponse)
async def auth_eligibility(
    request: AuthEligibilityRequest,
    http_request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    limiter: Annotated[
        AuthEligibilityRateLimiter,
        Depends(get_auth_eligibility_rate_limiter),
    ],
) -> AuthEligibilityResponse:
    try:
        normalized = normalize_user_identifier(request.identifier)
        await limiter.check(
            client_address=(http_request.client.host if http_request.client else "unknown"),
            normalized_identifier=normalized.value,
        )
        eligibility = await check_auth_eligibility(
            session,
            identifier=normalized.value,
        )
    except AuthRateLimitExceeded as error:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many sign-in attempts. Try again later.",
            headers={"Retry-After": str(error.retry_after_seconds)},
        ) from error
    except AuthUserNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Enter a valid phone number or email address",
        ) from error
    return AuthEligibilityResponse(
        kind=eligibility.kind.value,
        normalized_identifier=eligibility.normalized_identifier,
    )
