import hashlib
import hmac
from datetime import UTC, datetime
from typing import Any

import httpx
import jwt


class PlaidProviderError(RuntimeError):
    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


class PlaidClient:
    def __init__(
        self,
        *,
        client_id: str,
        secret: str,
        base_url: str,
        timeout_seconds: float = 15,
    ) -> None:
        self._client_id = client_id
        self._secret = secret
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._verification_keys: dict[str, dict[str, Any]] = {}

    async def create_link_token(
        self,
        *,
        client_user_id: str,
        client_name: str,
        country_codes: tuple[str, ...],
        webhook_url: str | None,
        redirect_uri: str | None,
        access_token: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "client_name": client_name,
            "language": "en",
            "country_codes": list(country_codes),
            "user": {"client_user_id": client_user_id},
        }
        if access_token is None:
            payload["products"] = ["transactions"]
            payload["transactions"] = {"days_requested": 180}
        else:
            payload["access_token"] = access_token
        if webhook_url:
            payload["webhook"] = webhook_url
        if redirect_uri:
            payload["redirect_uri"] = redirect_uri
        return await self._post("/link/token/create", payload)

    async def exchange_public_token(self, public_token: str) -> dict[str, Any]:
        return await self._post("/item/public_token/exchange", {"public_token": public_token})

    async def sync_transactions(
        self,
        *,
        access_token: str,
        cursor: str | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "access_token": access_token,
            "count": 500,
            "options": {"include_personal_finance_category": True},
        }
        if cursor:
            payload["cursor"] = cursor
        return await self._post("/transactions/sync", payload)

    async def remove_item(self, access_token: str) -> None:
        await self._post("/item/remove", {"access_token": access_token})

    async def verify_webhook(self, *, body: bytes, signed_jwt: str) -> bool:
        try:
            header = jwt.get_unverified_header(signed_jwt)
        except jwt.PyJWTError:
            return False
        if header.get("alg") != "ES256" or not isinstance(header.get("kid"), str):
            return False
        kid = header["kid"]
        key_data = self._verification_keys.get(kid)
        if key_data is None:
            try:
                response = await self._post("/webhook_verification_key/get", {"key_id": kid})
            except PlaidProviderError:
                return False
            candidate = response.get("key")
            if not isinstance(candidate, dict):
                return False
            key_data = candidate
            self._verification_keys[kid] = key_data
        try:
            key = jwt.PyJWK.from_dict(key_data).key
            claims = jwt.decode(
                signed_jwt,
                key,
                algorithms=["ES256"],
                options={"verify_aud": False, "require": ["iat", "request_body_sha256"]},
            )
        except (jwt.PyJWTError, ValueError):
            return False
        issued_at = claims.get("iat")
        body_hash = claims.get("request_body_sha256")
        if not isinstance(issued_at, int) or not isinstance(body_hash, str):
            return False
        age_seconds = datetime.now(UTC).timestamp() - issued_at
        if age_seconds < -30 or age_seconds > 300:
            return False
        actual_hash = hashlib.sha256(body).hexdigest()
        return hmac.compare_digest(actual_hash, body_hash)

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {
            "PLAID-CLIENT-ID": self._client_id,
            "PLAID-SECRET": self._secret,
            "Plaid-Version": "2020-09-14",
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout_seconds,
                headers=headers,
            ) as client:
                response = await client.post(path, json=payload)
        except httpx.HTTPError as error:
            raise PlaidProviderError("Plaid is temporarily unavailable") from error
        try:
            data = response.json()
        except ValueError as error:
            raise PlaidProviderError("Plaid returned an invalid response") from error
        if response.is_error:
            code = data.get("error_code") if isinstance(data, dict) else None
            message = (
                data.get("error_message") if isinstance(data, dict) else "Plaid request failed"
            )
            raise PlaidProviderError(
                str(message or "Plaid request failed"),
                code=code if isinstance(code, str) else None,
            )
        if not isinstance(data, dict):
            raise PlaidProviderError("Plaid returned an invalid response")
        return data
