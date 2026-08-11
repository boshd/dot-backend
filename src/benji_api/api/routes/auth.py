from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from benji_api.config import Settings, get_settings
from benji_api.db.session import get_session
from benji_api.integrations.stytch.client import StytchAuthClient, StytchProviderError
from benji_api.integrations.stytch.dependencies import get_stytch_client
from benji_api.schemas.phone import PhoneNumber
from benji_api.services.auth import AuthUserNotFoundError, start_phone_authentication

router = APIRouter(prefix="/auth", tags=["authentication"])


class PhoneAuthStartRequest(BaseModel):
    phone_number: PhoneNumber


class PhoneAuthStartResponse(BaseModel):
    method_id: str
    expires_in_seconds: int


@router.post("/otp/start", response_model=PhoneAuthStartResponse)
async def start_phone_auth(
    request: PhoneAuthStartRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    stytch_client: Annotated[StytchAuthClient | None, Depends(get_stytch_client)],
) -> PhoneAuthStartResponse:
    if stytch_client is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Stytch authentication is not configured",
        )
    try:
        challenge = await start_phone_authentication(
            session,
            phone_number=request.phone_number,
            client=stytch_client,
            settings=settings,
        )
    except AuthUserNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except StytchProviderError as error:
        response_status = (
            status.HTTP_429_TOO_MANY_REQUESTS
            if error.error_type == "too_many_requests"
            else status.HTTP_502_BAD_GATEWAY
        )
        raise HTTPException(
            status_code=response_status,
            detail="A verification code could not be sent",
        ) from error
    return PhoneAuthStartResponse(
        method_id=challenge.method_id,
        expires_in_seconds=challenge.expires_in_seconds,
    )
