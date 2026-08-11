from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from benji_api.api.router import build_api_router
from benji_api.config import Settings
from benji_api.main import app


@pytest.mark.anyio
async def test_receive_message() -> None:
    user_id = uuid4()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/v1/messages",
            json={
                "user_id": str(user_id),
                "channel": "web",
                "content": "Help me plan a weekend away.",
            },
        )

    assert response.status_code == 202
    body = response.json()
    assert UUID(body["message_id"])
    assert UUID(body["conversation_id"])
    assert body["status"] == "received"


@pytest.mark.anyio
async def test_placeholder_message_ingress_is_not_available_in_production() -> None:
    production_app = FastAPI()
    production_app.include_router(build_api_router(Settings(environment="production")))

    async with AsyncClient(
        transport=ASGITransport(app=production_app),
        base_url="http://test",
    ) as client:
        generic_response = await client.post("/api/v1/messages", json={})
        adapter_response = await client.post("/api/v1/inbound/messages", json={})

    assert generic_response.status_code == 404
    assert adapter_response.status_code == 404
