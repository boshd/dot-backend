from dataclasses import dataclass

import stytch
from stytch.core.response_base import StytchError


class StytchProviderError(RuntimeError):
    def __init__(self, message: str, *, error_type: str | None = None) -> None:
        super().__init__(message)
        self.error_type = error_type


@dataclass(frozen=True, slots=True)
class StytchOtpChallenge:
    method_id: str
    provider_user_id: str


class StytchAuthClient:
    def __init__(self, *, project_id: str, secret: str) -> None:
        self._client = stytch.Client(project_id=project_id, secret=secret)

    async def login_or_create_sms_otp(
        self,
        *,
        phone_number: str,
        expiration_minutes: int,
    ) -> StytchOtpChallenge:
        try:
            response = await self._client.otps.sms.login_or_create_async(
                phone_number=phone_number,
                expiration_minutes=expiration_minutes,
                create_user_as_pending=True,
            )
        except StytchError as error:
            raise _provider_error(error) from error
        return StytchOtpChallenge(
            method_id=response.phone_id,
            provider_user_id=response.user_id,
        )

    async def send_sms_otp(
        self,
        *,
        provider_user_id: str,
        phone_number: str,
        expiration_minutes: int,
    ) -> StytchOtpChallenge:
        try:
            response = await self._client.otps.sms.send_async(
                phone_number=phone_number,
                user_id=provider_user_id,
                expiration_minutes=expiration_minutes,
            )
        except StytchError as error:
            raise _provider_error(error) from error
        return StytchOtpChallenge(
            method_id=response.phone_id,
            provider_user_id=response.user_id,
        )

    async def authenticate_session_jwt(
        self,
        *,
        session_jwt: str,
        max_token_age_seconds: int,
    ) -> str:
        try:
            response = await self._client.sessions.authenticate_jwt_async(
                session_jwt=session_jwt,
                max_token_age_seconds=max_token_age_seconds,
            )
        except StytchError as error:
            raise _provider_error(error) from error
        except Exception as error:
            raise StytchProviderError("Stytch session validation failed") from error
        return response.session.user_id


def _provider_error(error: StytchError) -> StytchProviderError:
    return StytchProviderError(
        error.details.error_message,
        error_type=error.details.error_type,
    )
