import time
from datetime import UTC, datetime, timedelta

import httpx
import jwt
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from firebase_admin import auth

from benji_api.integrations.auth import AuthProviderError
from benji_api.integrations.firebase.client import (
    FirebaseAuthClient,
    _firebase_provider,
    _verified_token,
)
from benji_api.integrations.firebase.public_keys import FirebasePublicKeyCache, _cache_max_age


def _signing_keys() -> tuple[rsa.RSAPrivateKey, str]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Firebase Test")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .sign(private_key, hashes.SHA256())
        .public_bytes(serialization.Encoding.PEM)
    )
    return private_key, certificate.decode()


def _firebase_token(
    private_key: rsa.RSAPrivateKey,
    *,
    project_id: str = "dot-production",
    key_id: str = "firebase-key",
    **overrides: object,
) -> str:
    now = int(time.time())
    claims: dict[str, object] = {
        "aud": project_id,
        "iss": f"https://securetoken.google.com/{project_id}",
        "sub": "firebase-uid",
        "iat": now - 10,
        "exp": now + 300,
        "auth_time": now - 20,
        "email": "person@example.com",
        "email_verified": True,
    }
    claims.update(overrides)
    return jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": key_id})


@pytest.mark.anyio
async def test_firebase_client_verifies_id_token_and_revocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = object()
    received: list[tuple[str, object, bool]] = []

    def fake_verify_id_token(
        token: str,
        firebase_app: object,
        check_revoked: bool,
    ) -> dict[str, object]:
        received.append((token, firebase_app, check_revoked))
        return {"sub": "firebase-uid", "phone_number": "+14155552671"}

    monkeypatch.setattr(auth, "verify_id_token", fake_verify_id_token)
    client = FirebaseAuthClient.__new__(FirebaseAuthClient)
    client._app = app
    client._check_revoked = True
    client._provider = _firebase_provider("dot-production")

    verified = await client.verify_token("firebase-id-token")

    assert received == [("firebase-id-token", app, True)]
    assert verified.subject == "firebase-uid"
    assert verified.provider == _firebase_provider("dot-production")


@pytest.mark.anyio
async def test_keyless_client_verifies_firebase_signature_and_claims() -> None:
    private_key, public_key = _signing_keys()
    requests = 0

    def certificates(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(
            200,
            json={"firebase-key": public_key},
            headers={"Cache-Control": "public, max-age=3600"},
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(certificates)) as http_client:
        cache = FirebasePublicKeyCache(http_client=http_client)
        client = FirebaseAuthClient(
            project_id="dot-production",
            check_revoked=False,
            public_key_cache=cache,
        )
        token = _firebase_token(private_key)

        first = await client.verify_token(token)
        second = await client.verify_token(token)

    assert first == second
    assert first.provider == _firebase_provider("dot-production")
    assert first.subject == "firebase-uid"
    assert first.email == "person@example.com"
    assert requests == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    "claims",
    [
        {"aud": "another-project"},
        {"iss": "https://securetoken.google.com/another-project"},
        {"sub": ""},
        {"sub": "x" * 129},
        {"iat": "not-a-number"},
        {"exp": int(time.time()) - 1},
        {"auth_time": int(time.time()) + 300},
        {"auth_time": True},
        {"auth_time": float("inf")},
    ],
)
async def test_keyless_client_rejects_invalid_firebase_claims(
    claims: dict[str, object],
) -> None:
    private_key, public_key = _signing_keys()

    def certificates(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"firebase-key": public_key}, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(certificates)) as http_client:
        client = FirebaseAuthClient(
            project_id="dot-production",
            public_key_cache=FirebasePublicKeyCache(http_client=http_client),
        )
        with pytest.raises(AuthProviderError):
            await client.verify_token(_firebase_token(private_key, **claims))


@pytest.mark.anyio
async def test_keyless_client_rejects_malformed_headers_without_fetching_keys() -> None:
    requests = 0

    def certificates(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, json={}, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(certificates)) as http_client:
        client = FirebaseAuthClient(
            project_id="dot-production",
            public_key_cache=FirebasePublicKeyCache(http_client=http_client),
        )
        with pytest.raises(AuthProviderError):
            await client.verify_token("not-a-jwt")
        unsigned_token = jwt.encode(
            {"sub": "firebase-uid"},
            "not-a-firebase-key-but-long-enough",
            algorithm="HS256",
            headers={"kid": "firebase-key"},
        )
        with pytest.raises(AuthProviderError):
            await client.verify_token(unsigned_token)

    assert requests == 0


@pytest.mark.anyio
async def test_public_key_cache_respects_cache_control_max_age_and_age() -> None:
    clock = [100.0]
    requests = 0

    def certificates(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(
            200,
            json={"firebase-key": "certificate"},
            headers={"Cache-Control": "public, max-age=10", "Age": "2"},
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(certificates)) as http_client:
        cache = FirebasePublicKeyCache(http_client=http_client, clock=lambda: clock[0])
        assert await cache.certificate_for("firebase-key") == "certificate"
        clock[0] = 107.9
        assert await cache.certificate_for("firebase-key") == "certificate"
        clock[0] = 108.0
        assert await cache.certificate_for("firebase-key") == "certificate"

    assert requests == 2


def test_public_key_cache_does_not_cache_no_store_responses() -> None:
    assert _cache_max_age({"cache-control": "public, max-age=3600, no-store"}) == 0


def test_revocation_checks_require_explicit_credentials() -> None:
    with pytest.raises(ValueError, match="FIREBASE_SERVICE_ACCOUNT_JSON"):
        FirebaseAuthClient(project_id="dot-production", check_revoked=True)


def test_firebase_identity_namespace_is_project_scoped() -> None:
    production = _firebase_provider("dot-production")
    staging = _firebase_provider("dot-staging")

    assert production.startswith("firebase:")
    assert len(production) <= 32
    assert production != staging


def test_firebase_claims_require_verified_email() -> None:
    verified = _verified_token(
        {
            "sub": "firebase-uid",
            "phone_number": "+14155552671",
            "email": "person@example.com",
            "email_verified": False,
        }
    )

    assert verified.provider == "firebase"
    assert verified.subject == "firebase-uid"
    assert verified.phone_number == "+14155552671"
    assert verified.email is None


def test_firebase_claims_accept_verified_email() -> None:
    verified = _verified_token(
        {
            "uid": "firebase-uid",
            "email": "person@example.com",
            "email_verified": True,
        }
    )

    assert verified.email == "person@example.com"


def test_firebase_claims_require_subject() -> None:
    with pytest.raises(AuthProviderError):
        _verified_token({"email": "person@example.com", "email_verified": True})
