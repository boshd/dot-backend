import asyncio
import time
from collections.abc import Callable, Mapping
from typing import Any

import httpx

from benji_api.integrations.auth import AuthProviderError

GOOGLE_SECURE_TOKEN_CERTIFICATES_URL = (
    "https://www.googleapis.com/robot/v1/metadata/x509/"
    "securetoken@system.gserviceaccount.com"
)
_DEFAULT_CACHE_SECONDS = 300


class FirebasePublicKeyCache:
    """Cache Google's Firebase signing certificates for their advertised lifetime."""

    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._http_client = http_client
        self._clock = clock
        self._certificates: Mapping[str, str] = {}
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    async def certificate_for(self, key_id: str) -> str:
        if not key_id:
            raise AuthProviderError("Firebase ID token has no signing key ID")

        async with self._lock:
            if not self._certificates or self._clock() >= self._expires_at:
                await self._refresh()
            certificate = self._certificates.get(key_id)

        if certificate is None:
            raise AuthProviderError("Firebase ID token uses an unknown signing key")
        return certificate

    async def _refresh(self) -> None:
        try:
            if self._http_client is None:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    response = await client.get(GOOGLE_SECURE_TOKEN_CERTIFICATES_URL)
            else:
                response = await self._http_client.get(GOOGLE_SECURE_TOKEN_CERTIFICATES_URL)
            response.raise_for_status()
            payload: Any = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise AuthProviderError("Firebase signing certificates are unavailable") from error

        if not isinstance(payload, dict):
            raise AuthProviderError("Firebase signing certificate response is invalid")
        certificates = {
            key_id: certificate
            for key_id, certificate in payload.items()
            if isinstance(key_id, str)
            and key_id
            and isinstance(certificate, str)
            and certificate.strip()
        }
        if not certificates or len(certificates) != len(payload):
            raise AuthProviderError("Firebase signing certificate response is invalid")

        max_age = _cache_max_age(response.headers)
        self._certificates = certificates
        self._expires_at = self._clock() + max_age


def _cache_max_age(headers: Mapping[str, str]) -> int:
    cache_control = headers.get("cache-control", "")
    directives = [directive.strip().partition("=") for directive in cache_control.split(",")]
    if any(name.casefold() in {"no-cache", "no-store"} for name, _, _ in directives):
        return 0
    for name, separator, raw_value in directives:
        if separator and name.casefold() == "max-age":
            try:
                max_age = max(0, int(raw_value.strip().strip('"')))
                age = max(0, int(headers.get("age", "0")))
            except ValueError:
                return 0
            return max(0, max_age - age)
    return _DEFAULT_CACHE_SECONDS
