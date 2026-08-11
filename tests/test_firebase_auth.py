import pytest
from firebase_admin import auth

from benji_api.integrations.auth import AuthProviderError
from benji_api.integrations.firebase.client import (
    FirebaseAuthClient,
    _firebase_provider,
    _verified_token,
)


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
