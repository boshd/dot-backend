import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from benji_api.api.dependencies import get_waitlist_rate_limiter
from benji_api.db.base import Base
from benji_api.db.session import get_session
from benji_api.main import app
from benji_api.models.user import User
from benji_api.models.waitlist import WaitlistEntry
from benji_api.services.waitlist import get_waitlist_referral_stats, join_waitlist
from benji_api.services.waitlist_rate_limit import WaitlistRateLimiter


@pytest.mark.anyio
async def test_waitlist_join_is_idempotent_and_attributes_one_referrer() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async with session_factory() as session:
        first = await join_waitlist(
            session,
            email="  FIRST@Example.com ",
            source="landing",
            utm_source="friend",
        )
        second = await join_waitlist(
            session,
            email="second@example.com",
            referral_code=first.entry.referral_code,
            utm_campaign="launch",
        )
        duplicate = await join_waitlist(
            session,
            email="SECOND@example.com",
            referral_code="does-not-reassign",
            source="duplicate",
        )
        first_stats = await get_waitlist_referral_stats(
            session,
            referral_code=first.entry.referral_code,
        )
        entries = (
            await session.scalars(select(WaitlistEntry).order_by(WaitlistEntry.created_at))
        ).all()
        user_count = await session.scalar(select(func.count()).select_from(User))

    await engine.dispose()

    assert first.joined is True
    assert first.position == 1
    assert second.joined is True
    assert second.position == 2
    assert duplicate.joined is False
    assert duplicate.entry.id == second.entry.id
    assert first_stats.referral_count == 1
    assert len(entries) == 2
    assert entries[0].normalized_email == "first@example.com"
    assert entries[0].source == "landing"
    assert entries[0].utm_source == "friend"
    assert entries[1].referred_by_id == entries[0].id
    assert entries[1].utm_campaign == "launch"
    assert user_count == 0


@pytest.mark.anyio
async def test_successful_referral_moves_referrer_ahead_in_waitlist() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async with session_factory() as session:
        early = await join_waitlist(session, email="early@example.com")
        mover = await join_waitlist(session, email="mover@example.com")
        assert early.position == 1
        assert mover.position == 2

        referral = await join_waitlist(
            session,
            email="friend@example.com",
            referral_code=mover.entry.referral_code,
        )
        moved_stats = await get_waitlist_referral_stats(
            session,
            referral_code=mover.entry.referral_code,
        )
        early_stats = await get_waitlist_referral_stats(
            session,
            referral_code=early.entry.referral_code,
        )

    await engine.dispose()

    assert referral.joined is True
    assert moved_stats.referral_count == 1
    assert moved_stats.position == 1
    assert early_stats.referral_count == 0
    assert early_stats.position == 2


@pytest.mark.anyio
async def test_waitlist_api_joins_and_returns_public_referral_stats() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    limiter = WaitlistRateLimiter(
        ip_per_minute=20,
        ip_per_hour=100,
        email_per_hour=20,
    )

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async def override_get_session():  # type: ignore[no-untyped-def]
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[get_waitlist_rate_limiter] = lambda: limiter
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app, client=("192.0.2.10", 123)),
            base_url="http://test",
        ) as client:
            first_response = await client.post(
                "/api/v1/waitlist",
                json={"email": "first@example.com", "source": "hero"},
            )
            first_payload = first_response.json()
            referred_response = await client.post(
                "/api/v1/waitlist",
                json={
                    "email": "second@example.com",
                    "referral_code": first_payload["referral_code"],
                    "utm_medium": "share",
                },
            )
            duplicate_response = await client.post(
                "/api/v1/waitlist",
                json={"email": "FIRST@example.com"},
            )
            stats_response = await client.get(
                f"/api/v1/waitlist/referrals/{first_payload['referral_code']}"
            )
            missing_response = await client.get(
                "/api/v1/waitlist/referrals/not-a-real-code"
            )
            invalid_email_response = await client.post(
                "/api/v1/waitlist",
                json={"email": "not-an-email"},
            )
    finally:
        app.dependency_overrides.pop(get_waitlist_rate_limiter, None)
        app.dependency_overrides.pop(get_session, None)
        await engine.dispose()

    assert first_response.status_code == 200
    assert first_payload["joined"] is True
    assert first_payload["position"] == 1
    assert first_payload["referral_count"] == 0
    assert "email" not in first_payload
    assert referred_response.status_code == 200
    assert referred_response.json()["joined"] is True
    assert duplicate_response.status_code == 200
    assert duplicate_response.json() == {**first_payload, "joined": False, "referral_count": 1}
    assert stats_response.status_code == 200
    assert stats_response.json() == {"position": 1, "referral_count": 1}
    assert missing_response.status_code == 404
    assert invalid_email_response.status_code == 422


@pytest.mark.anyio
async def test_waitlist_api_returns_retry_after_when_rate_limited() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    limiter = WaitlistRateLimiter(
        ip_per_minute=1,
        ip_per_hour=10,
        email_per_hour=10,
    )

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async def override_get_session():  # type: ignore[no-untyped-def]
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[get_waitlist_rate_limiter] = lambda: limiter
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app, client=("192.0.2.20", 123)),
            base_url="http://test",
        ) as client:
            first_response = await client.post(
                "/api/v1/waitlist",
                json={"email": "first@example.com"},
            )
            limited_response = await client.post(
                "/api/v1/waitlist",
                json={"email": "second@example.com"},
            )
    finally:
        app.dependency_overrides.pop(get_waitlist_rate_limiter, None)
        app.dependency_overrides.pop(get_session, None)
        await engine.dispose()

    assert first_response.status_code == 200
    assert limited_response.status_code == 429
    assert limited_response.headers["retry-after"] == "60"
