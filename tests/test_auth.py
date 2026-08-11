from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from benji_api.config import Settings, get_settings
from benji_api.db.base import Base
from benji_api.db.session import get_session
from benji_api.integrations.stytch.client import StytchOtpChallenge
from benji_api.integrations.stytch.dependencies import get_stytch_client
from benji_api.main import app
from benji_api.models import AuthIdentity, User


class FakeStytchClient:
    def __init__(self) -> None:
        self.login_or_create_for: list[str] = []
        self.sent_to: list[tuple[str, str]] = []

    async def login_or_create_sms_otp(
        self,
        *,
        phone_number: str,
        expiration_minutes: int,
    ) -> StytchOtpChallenge:
        assert expiration_minutes == 5
        self.login_or_create_for.append(phone_number)
        return StytchOtpChallenge(
            method_id="phone-test-benji",
            provider_user_id="user-test-benji",
        )

    async def send_sms_otp(
        self,
        *,
        provider_user_id: str,
        phone_number: str,
        expiration_minutes: int,
    ) -> StytchOtpChallenge:
        assert expiration_minutes == 5
        self.sent_to.append((provider_user_id, phone_number))
        return StytchOtpChallenge(
            method_id="phone-test-benji",
            provider_user_id=provider_user_id,
        )

    async def authenticate_session_jwt(
        self,
        *,
        session_jwt: str,
        max_token_age_seconds: int,
    ) -> str:
        assert session_jwt == "valid-session-jwt"
        assert max_token_age_seconds == 60
        return "user-test-benji"


@asynccontextmanager
async def auth_test_app() -> AsyncIterator[
    tuple[AsyncClient, async_sessionmaker[AsyncSession], FakeStytchClient]
]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    fake_stytch = FakeStytchClient()
    settings = Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        web_chat_dev_identity_enabled=False,
        stytch_project_id="project-test-benji",
        stytch_secret="secret-test-benji",
    )

    async def override_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_stytch_client] = lambda: fake_stytch
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            yield client, session_factory, fake_stytch
    finally:
        app.dependency_overrides.clear()
        await engine.dispose()


@pytest.mark.anyio
async def test_existing_user_can_start_otp_and_open_authenticated_chat() -> None:
    phone = "+14155552671"
    async with auth_test_app() as (client, session_factory, fake_stytch):
        async with session_factory() as session:
            user = User(phone_number=phone)
            session.add(user)
            await session.commit()

        started = await client.post(
            "/api/v1/auth/otp/start",
            json={"phone_number": phone},
        )
        assert started.status_code == 200
        assert started.json() == {
            "method_id": "phone-test-benji",
            "expires_in_seconds": 300,
        }
        assert fake_stytch.login_or_create_for == [phone]
        assert fake_stytch.sent_to == []

        restarted = await client.post(
            "/api/v1/auth/otp/start",
            json={"phone_number": phone},
        )
        assert restarted.status_code == 200
        assert fake_stytch.login_or_create_for == [phone]
        assert fake_stytch.sent_to == [("user-test-benji", phone)]

        anonymous = await client.post("/api/v1/web/chat/session", json={})
        assert anonymous.status_code == 401

        authenticated = await client.post(
            "/api/v1/web/chat/session",
            json={},
            headers={"Authorization": "Bearer valid-session-jwt"},
        )
        assert authenticated.status_code == 200
        assert authenticated.json()["user"]["user_id"] == str(user.id)

        async with session_factory() as session:
            identity = await session.scalar(select(AuthIdentity))
        assert identity is not None
        assert identity.provider_subject == "user-test-benji"
        assert identity.verified_phone == phone
        assert identity.verified_at is not None


@pytest.mark.anyio
async def test_phone_auth_does_not_create_web_only_benji_users() -> None:
    async with auth_test_app() as (client, session_factory, fake_stytch):
        response = await client.post(
            "/api/v1/auth/otp/start",
            json={"phone_number": "+14155552671"},
        )

        assert response.status_code == 404
        assert "Message Dot first" in response.json()["detail"]
        assert fake_stytch.login_or_create_for == []
        async with session_factory() as session:
            assert await session.scalar(select(User)) is None
