from dataclasses import dataclass
from typing import Protocol


class AuthProviderError(RuntimeError):
    """A presented credential could not be verified by its auth provider."""


@dataclass(frozen=True, slots=True)
class VerifiedAuthToken:
    provider: str
    subject: str
    phone_number: str | None = None
    email: str | None = None


class AuthTokenVerifier(Protocol):
    async def verify_token(self, token: str) -> VerifiedAuthToken: ...
