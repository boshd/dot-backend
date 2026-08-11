import asyncio
import json
from collections.abc import Mapping
from hashlib import sha256
from typing import Any

import firebase_admin
from firebase_admin import auth, credentials

from benji_api.integrations.auth import AuthProviderError, VerifiedAuthToken

FIREBASE_PROVIDER = "firebase"


class FirebaseAuthClient:
    def __init__(
        self,
        *,
        project_id: str,
        service_account_json: str | None = None,
        check_revoked: bool = True,
    ) -> None:
        self._check_revoked = check_revoked
        self._provider = _firebase_provider(project_id)
        app_name_seed = f"{project_id}:{service_account_json or 'application-default'}"
        app_name = f"dot-{sha256(app_name_seed.encode()).hexdigest()[:16]}"
        try:
            self._app = firebase_admin.get_app(app_name)
        except ValueError:
            credential = _firebase_credential(service_account_json)
            self._app = firebase_admin.initialize_app(
                credential,
                {"projectId": project_id},
                name=app_name,
            )

    async def verify_token(self, token: str) -> VerifiedAuthToken:
        if not token.strip():
            raise AuthProviderError("Firebase ID token is empty")
        try:
            claims = await asyncio.to_thread(
                auth.verify_id_token,
                token,
                self._app,
                self._check_revoked,
            )
        except Exception as error:
            raise AuthProviderError("Firebase ID token verification failed") from error
        return _verified_token(claims, provider=self._provider)


def _firebase_credential(service_account_json: str | None) -> credentials.Base:
    if service_account_json is None:
        return credentials.ApplicationDefault()
    try:
        payload = json.loads(service_account_json)
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError("FIREBASE_SERVICE_ACCOUNT_JSON must be valid JSON") from error
    if not isinstance(payload, dict):
        raise ValueError("FIREBASE_SERVICE_ACCOUNT_JSON must contain a JSON object")
    return credentials.Certificate(payload)


def _firebase_provider(project_id: str) -> str:
    return f"{FIREBASE_PROVIDER}:{sha256(project_id.encode()).hexdigest()[:16]}"


def _verified_token(
    claims: Mapping[str, Any],
    *,
    provider: str = FIREBASE_PROVIDER,
) -> VerifiedAuthToken:
    subject = claims.get("uid") or claims.get("sub")
    if not isinstance(subject, str) or not subject.strip():
        raise AuthProviderError("Firebase ID token has no subject")

    phone_number = claims.get("phone_number")
    verified_phone = phone_number if isinstance(phone_number, str) else None

    email = claims.get("email")
    verified_email = (
        email if isinstance(email, str) and claims.get("email_verified") is True else None
    )
    return VerifiedAuthToken(
        provider=provider,
        subject=subject,
        phone_number=verified_phone,
        email=verified_email,
    )
