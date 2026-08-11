import asyncio
import json
import math
import time
from collections.abc import Mapping
from hashlib import sha256
from typing import Any

import firebase_admin
import jwt
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import rsa
from firebase_admin import auth, credentials

from benji_api.integrations.auth import AuthProviderError, VerifiedAuthToken
from benji_api.integrations.firebase.public_keys import FirebasePublicKeyCache

FIREBASE_PROVIDER = "firebase"
_MAX_ID_TOKEN_LENGTH = 16_384


class FirebaseAuthClient:
    def __init__(
        self,
        *,
        project_id: str,
        service_account_json: str | None = None,
        check_revoked: bool = False,
        public_key_cache: FirebasePublicKeyCache | None = None,
    ) -> None:
        if not project_id.strip():
            raise ValueError("FIREBASE_PROJECT_ID cannot be empty")
        if check_revoked and service_account_json is None:
            raise ValueError(
                "FIREBASE_CHECK_REVOKED requires FIREBASE_SERVICE_ACCOUNT_JSON; "
                "set it to false for keyless verification"
            )

        self._project_id = project_id
        self._check_revoked = check_revoked
        self._provider = _firebase_provider(project_id)
        self._public_key_cache = public_key_cache or FirebasePublicKeyCache()
        self._app: firebase_admin.App | None = None

        if service_account_json is not None:
            app_name_seed = f"{project_id}:{service_account_json}"
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
        if not isinstance(token, str) or not token.strip():
            raise AuthProviderError("Firebase ID token is empty")
        if len(token) > _MAX_ID_TOKEN_LENGTH:
            raise AuthProviderError("Firebase ID token is malformed")

        if self._app is not None:
            try:
                claims = await asyncio.to_thread(
                    auth.verify_id_token,
                    token,
                    self._app,
                    self._check_revoked,
                )
            except Exception as error:
                raise AuthProviderError("Firebase ID token verification failed") from error
        else:
            claims = await self._verify_keyless(token)
        return _verified_token(claims, provider=self._provider)

    async def _verify_keyless(self, token: str) -> Mapping[str, Any]:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as error:
            raise AuthProviderError("Firebase ID token is malformed") from error

        if header.get("alg") != "RS256":
            raise AuthProviderError("Firebase ID token uses an invalid algorithm")
        key_id = header.get("kid")
        if not isinstance(key_id, str) or not key_id:
            raise AuthProviderError("Firebase ID token has no signing key ID")

        certificate = await self._public_key_cache.certificate_for(key_id)
        try:
            signing_key = x509.load_pem_x509_certificate(certificate.encode()).public_key()
        except ValueError as error:
            raise AuthProviderError("Firebase signing certificate is invalid") from error
        if not isinstance(signing_key, rsa.RSAPublicKey):
            raise AuthProviderError("Firebase signing certificate does not contain an RSA key")

        issuer = f"https://securetoken.google.com/{self._project_id}"
        try:
            claims = jwt.decode(
                token,
                signing_key,
                algorithms=["RS256"],
                audience=self._project_id,
                issuer=issuer,
                options={
                    "require": ["auth_time", "aud", "exp", "iat", "iss", "sub"],
                },
            )
        except (jwt.PyJWTError, OverflowError, TypeError, ValueError) as error:
            raise AuthProviderError("Firebase ID token verification failed") from error

        _validate_firebase_claims(claims, project_id=self._project_id)
        return claims


def _firebase_credential(service_account_json: str) -> credentials.Base:
    try:
        payload = json.loads(service_account_json)
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError("FIREBASE_SERVICE_ACCOUNT_JSON must be valid JSON") from error
    if not isinstance(payload, dict):
        raise ValueError("FIREBASE_SERVICE_ACCOUNT_JSON must contain a JSON object")
    return credentials.Certificate(payload)


def _firebase_provider(project_id: str) -> str:
    return f"{FIREBASE_PROVIDER}:{sha256(project_id.encode()).hexdigest()[:16]}"


def _validate_firebase_claims(claims: Mapping[str, Any], *, project_id: str) -> None:
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject.strip() or len(subject) > 128:
        raise AuthProviderError("Firebase ID token has an invalid subject")
    if claims.get("aud") != project_id:
        raise AuthProviderError("Firebase ID token has an invalid audience")
    if claims.get("iss") != f"https://securetoken.google.com/{project_id}":
        raise AuthProviderError("Firebase ID token has an invalid issuer")

    now = time.time()
    issued_at = _numeric_date(claims, "iat")
    expires_at = _numeric_date(claims, "exp")
    authenticated_at = _numeric_date(claims, "auth_time")
    if issued_at > now or authenticated_at > now or expires_at <= now:
        raise AuthProviderError("Firebase ID token has invalid timestamps")


def _numeric_date(claims: Mapping[str, Any], name: str) -> int | float:
    value = claims.get(name)
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise AuthProviderError(f"Firebase ID token has an invalid {name} claim")
    return value


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
