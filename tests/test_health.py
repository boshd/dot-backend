import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from benji_api.db.session import get_session
from benji_api.main import app


@pytest.mark.anyio
async def test_health() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "benji-api"}


@pytest.mark.anyio
async def test_readiness_checks_database() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async def override_get_session():  # type: ignore[no-untyped-def]
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.get("/ready")
    finally:
        app.dependency_overrides.pop(get_session, None)
        await engine.dispose()

    assert response.status_code == 200
    assert response.json() == {"status": "ready", "service": "benji-api"}
