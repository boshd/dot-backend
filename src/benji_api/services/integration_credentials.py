import json
from typing import Any

from cryptography.fernet import Fernet, InvalidToken


class IntegrationCredentialError(RuntimeError):
    pass


class IntegrationCredentialVault:
    def __init__(self, encryption_key: str) -> None:
        try:
            self._fernet = Fernet(encryption_key.encode())
        except (TypeError, ValueError) as error:
            raise IntegrationCredentialError(
                "INTEGRATION_TOKEN_ENCRYPTION_KEY must be a valid Fernet key"
            ) from error

    def encrypt(self, credentials: dict[str, Any]) -> str:
        serialized = json.dumps(credentials, separators=(",", ":")).encode()
        return self._fernet.encrypt(serialized).decode()

    def decrypt(self, ciphertext: str) -> dict[str, Any]:
        try:
            payload = json.loads(self._fernet.decrypt(ciphertext.encode()))
        except (InvalidToken, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise IntegrationCredentialError(
                "Stored integration credentials are invalid"
            ) from error
        if not isinstance(payload, dict):
            raise IntegrationCredentialError("Stored integration credentials are invalid")
        return payload
