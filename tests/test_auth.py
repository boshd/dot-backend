from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from benji_api.api.dependencies import get_auth_eligibility_rate_limiter
from benji_api.config import Settings, get_settings
from benji_api.db.base import Base
from benji_api.db.session import get_session
from benji_api.integrations.auth import AuthProviderError, VerifiedAuthToken
from benji_api.integrations.firebase.dependencies import get_auth_token_verifier
from benji_api.main import app
from benji_api.models import AuthIdentity, User
from benji_api.services.auth_rate_limit import AuthEligibilityRateLimiter
from benji_api.services.users import find_user_by_identifier, resolve_user_from_identifier


class FakeAuthTokenVerifier:
    def __init__(self) -> None:
        self.tokens: dict[str, VerifiedAuthToken] = {}

    async def verify_token(self, token: str) -> VerifiedAuthToken:
        if token not in self.tokens:
            raise AuthProviderError("invalid token")
        return self.tokens[token]


@asynccontextmanager
async def auth_test_app() -> AsyncIterator[
    tuple[AsyncClient, async_sessionmaker[AsyncSession], FakeAuthTokenVerifier]
]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    verifier = FakeAuthTokenVerifier()
    settings = Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        web_chat_dev_identity_enabled=False,
        firebase_project_id="dot-test",
    )

    async def override_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_auth_token_verifier] = lambda: verifier
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            yield client, session_factory, verifier
    finally:
        app.dependency_overrides.clear()
        await engine.dispose()


async def _create_messaging_user(
    session_factory: async_sessionmaker[AsyncSession],
    identifier: str,
) -> User:
    async with session_factory() as session:
        resolution = await resolve_user_from_identifier(
            session,
            identifier,
            source="linq",
        )
        await session.commit()
        return resolution.user


@pytest.mark.anyio
async def test_existing_phone_user_can_sign_in_with_firebase() -> None:
    phone = "+14155552671"
    async with auth_test_app() as (client, session_factory, verifier):
        user = await _create_messaging_user(session_factory, phone)
        verifier.tokens["valid-phone-token"] = VerifiedAuthToken(
            provider="firebase",
            subject="firebase-phone-uid",
            phone_number=phone,
        )

        eligible = await client.post(
            "/api/v1/auth/eligibility",
            json={"identifier": phone},
        )
        assert eligible.status_code == 200
        assert eligible.json() == {
            "eligible": True,
            "kind": "phone",
            "normalized_identifier": phone,
        }

        anonymous = await client.post("/api/v1/web/chat/session", json={})
        assert anonymous.status_code == 401

        authenticated = await client.post(
            "/api/v1/web/chat/session",
            json={},
            headers={"Authorization": "Bearer valid-phone-token"},
        )
        assert authenticated.status_code == 200
        assert authenticated.json()["user"]["user_id"] == str(user.id)

        async with session_factory() as session:
            identity = await session.scalar(select(AuthIdentity))
        assert identity is not None
        assert identity.provider == "firebase"
        assert identity.provider_subject == "firebase-phone-uid"
        assert identity.verified_phone == phone
        assert identity.verified_at is not None


@pytest.mark.anyio
async def test_existing_email_user_can_sign_in_with_firebase() -> None:
    email = "Kareem@Example.COM"
    async with auth_test_app() as (client, session_factory, verifier):
        user = await _create_messaging_user(session_factory, email)
        verifier.tokens["valid-email-token"] = VerifiedAuthToken(
            provider="firebase",
            subject="firebase-email-uid",
            email="kareem@example.com",
        )

        eligible = await client.post(
            "/api/v1/auth/eligibility",
            json={"identifier": email},
        )
        assert eligible.status_code == 200
        assert eligible.json() == {
            "eligible": True,
            "kind": "email",
            "normalized_identifier": "kareem@example.com",
        }

        authenticated = await client.post(
            "/api/v1/apps/catalog",
            json={},
            headers={"Authorization": "Bearer valid-email-token"},
        )
        assert authenticated.status_code == 200
        assert authenticated.json() == {"apps": []}
        async with session_factory() as session:
            identity = await session.scalar(select(AuthIdentity))
        assert identity is not None
        assert identity.user_id == user.id


@pytest.mark.anyio
async def test_auth_does_not_create_web_only_users() -> None:
    async with auth_test_app() as (client, session_factory, verifier):
        verifier.tokens["unknown-user-token"] = VerifiedAuthToken(
            provider="firebase",
            subject="firebase-unknown-uid",
            phone_number="+14155552671",
        )

        eligibility = await client.post(
            "/api/v1/auth/eligibility",
            json={"identifier": "+14155552671"},
        )
        assert eligibility.status_code == 404
        assert "Message Dot first" in eligibility.json()["detail"]

        authenticated = await client.post(
            "/api/v1/web/chat/session",
            json={},
            headers={"Authorization": "Bearer unknown-user-token"},
        )
        assert authenticated.status_code == 401

        async with session_factory() as session:
            assert await session.scalar(select(func.count()).select_from(User)) == 0
            assert await session.scalar(select(func.count()).select_from(AuthIdentity)) == 0


@pytest.mark.anyio
async def test_verified_claims_cannot_merge_two_dot_users() -> None:
    async with auth_test_app() as (client, session_factory, verifier):
        await _create_messaging_user(session_factory, "+14155552671")
        await _create_messaging_user(session_factory, "person@example.com")
        verifier.tokens["conflicting-token"] = VerifiedAuthToken(
            provider="firebase",
            subject="firebase-conflicting-uid",
            phone_number="+14155552671",
            email="person@example.com",
        )

        response = await client.post(
            "/api/v1/web/chat/session",
            json={},
            headers={"Authorization": "Bearer conflicting-token"},
        )
        assert response.status_code == 401
        async with session_factory() as session:
            assert await session.scalar(select(func.count()).select_from(AuthIdentity)) == 0


@pytest.mark.anyio
async def test_existing_active_firebase_identity_is_authoritative() -> None:
    async with auth_test_app() as (client, session_factory, verifier):
        user = await _create_messaging_user(session_factory, "person@example.com")
        async with session_factory() as session:
            session.add(
                AuthIdentity(
                    user_id=user.id,
                    provider="firebase",
                    provider_subject="existing-uid",
                    verified_at=datetime.now(UTC),
                )
            )
            await session.commit()
        verifier.tokens["existing-token"] = VerifiedAuthToken(
            provider="firebase",
            subject="existing-uid",
        )

        response = await client.post(
            "/api/v1/apps/catalog",
            json={},
            headers={"Authorization": "Bearer existing-token"},
        )
        assert response.status_code == 200
        assert response.json() == {"apps": []}


@pytest.mark.anyio
async def test_one_user_can_have_multiple_firebase_subjects() -> None:
    phone = "+14155552671"
    async with auth_test_app() as (client, session_factory, verifier):
        await _create_messaging_user(session_factory, phone)
        verifier.tokens["token-one"] = VerifiedAuthToken(
            provider="firebase",
            subject="firebase-uid-one",
            phone_number=phone,
        )
        verifier.tokens["token-two"] = VerifiedAuthToken(
            provider="firebase",
            subject="firebase-uid-two",
            phone_number=phone,
        )

        for token in ("token-one", "token-two"):
            response = await client.post(
                "/api/v1/web/chat/session",
                json={},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert response.status_code == 200

        async with session_factory() as session:
            assert await session.scalar(select(func.count()).select_from(AuthIdentity)) == 2


@pytest.mark.anyio
async def test_verified_secondary_claim_is_attached_to_the_canonical_user() -> None:
    phone = "+14155552671"
    email = "person@example.com"
    async with auth_test_app() as (client, session_factory, verifier):
        user = await _create_messaging_user(session_factory, phone)
        verifier.tokens["linked-claims-token"] = VerifiedAuthToken(
            provider="firebase:test-project",
            subject="linked-claims-uid",
            phone_number=phone,
            email=email,
        )

        response = await client.post(
            "/api/v1/web/chat/session",
            json={},
            headers={"Authorization": "Bearer linked-claims-token"},
        )
        assert response.status_code == 200

        async with session_factory() as session:
            linked_user = await find_user_by_identifier(session, email)
            assert linked_user is not None and linked_user.id == user.id


@pytest.mark.anyio
async def test_invalid_firebase_token_is_rejected() -> None:
    async with auth_test_app() as (client, _, _verifier):
        response = await client.post(
            "/api/v1/web/chat/session",
            json={},
            headers={"Authorization": "Bearer invalid-token"},
        )
        assert response.status_code == 401
        assert "invalid or is not linked" in response.json()["detail"]


@pytest.mark.anyio
async def test_eligibility_rejects_invalid_identifier() -> None:
    async with auth_test_app() as (client, _, _verifier):
        response = await client.post(
            "/api/v1/auth/eligibility",
            json={"identifier": "not an identifier"},
        )
        assert response.status_code == 422


@pytest.mark.anyio
async def test_eligibility_is_rate_limited_before_account_enumeration() -> None:
    async with auth_test_app() as (client, session_factory, _verifier):
        phone = "+14155552671"
        await _create_messaging_user(session_factory, phone)
        limiter = AuthEligibilityRateLimiter(
            ip_per_minute=1,
            ip_per_hour=10,
            identifier_per_hour=10,
        )
        app.dependency_overrides[get_auth_eligibility_rate_limiter] = lambda: limiter

        first = await client.post("/api/v1/auth/eligibility", json={"identifier": phone})
        second = await client.post("/api/v1/auth/eligibility", json={"identifier": phone})

        assert first.status_code == 200
        assert second.status_code == 429
        assert int(second.headers["retry-after"]) >= 1
