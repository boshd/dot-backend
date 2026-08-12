from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from benji_api.api.dependencies import get_waitlist_rate_limiter
from benji_api.db.session import get_session
from benji_api.services.users import normalize_email_address
from benji_api.services.waitlist import (
    WaitlistEntryNotFoundError,
    WaitlistResult,
    get_waitlist_referral_stats,
    join_waitlist,
)
from benji_api.services.waitlist_rate_limit import (
    WaitlistRateLimiter,
    WaitlistRateLimitExceeded,
)

router = APIRouter(prefix="/waitlist", tags=["waitlist"])


class WaitlistJoinRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    referral_code: str | None = Field(default=None, max_length=64)
    source: str | None = Field(default=None, max_length=64)
    utm_source: str | None = Field(default=None, max_length=120)
    utm_medium: str | None = Field(default=None, max_length=120)
    utm_campaign: str | None = Field(default=None, max_length=200)
    utm_term: str | None = Field(default=None, max_length=200)
    utm_content: str | None = Field(default=None, max_length=200)


class WaitlistResponse(BaseModel):
    joined: bool
    position: int = Field(ge=1)
    referral_code: str
    referral_count: int = Field(ge=0)


class WaitlistReferralStatsResponse(BaseModel):
    position: int = Field(ge=1)
    referral_count: int = Field(ge=0)


@router.post("", response_model=WaitlistResponse)
async def join_public_waitlist(
    request: WaitlistJoinRequest,
    http_request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    limiter: Annotated[WaitlistRateLimiter, Depends(get_waitlist_rate_limiter)],
) -> WaitlistResponse:
    try:
        normalized_email = normalize_email_address(request.email)
        await limiter.check(
            client_address=(http_request.client.host if http_request.client else "unknown"),
            normalized_email=normalized_email,
        )
        result = await join_waitlist(
            session,
            email=normalized_email,
            referral_code=request.referral_code,
            source=request.source,
            utm_source=request.utm_source,
            utm_medium=request.utm_medium,
            utm_campaign=request.utm_campaign,
            utm_term=request.utm_term,
            utm_content=request.utm_content,
        )
    except WaitlistRateLimitExceeded as error:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many waitlist attempts. Try again later.",
            headers={"Retry-After": str(error.retry_after_seconds)},
        ) from error
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Enter a valid email address",
        ) from error
    return _join_response(result)


@router.get(
    "/referrals/{referral_code}",
    response_model=WaitlistReferralStatsResponse,
)
async def waitlist_referral_stats(
    referral_code: Annotated[str, Path(min_length=8, max_length=64)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> WaitlistReferralStatsResponse:
    try:
        result = await get_waitlist_referral_stats(session, referral_code=referral_code)
    except WaitlistEntryNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    return WaitlistReferralStatsResponse(
        position=result.position,
        referral_count=result.referral_count,
    )


def _join_response(result: WaitlistResult) -> WaitlistResponse:
    return WaitlistResponse(
        joined=result.joined,
        position=result.position,
        referral_code=result.entry.referral_code,
        referral_count=result.referral_count,
    )
