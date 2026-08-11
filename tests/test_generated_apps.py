import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from benji_api.agents.tools import CreateGeneratedAppTool
from benji_api.agents.types import ToolContext
from benji_api.config import Settings, get_settings
from benji_api.db.base import Base
from benji_api.db.session import get_session
from benji_api.main import app
from benji_api.models.channel import Conversation, ConversationKind, ConversationMember
from benji_api.models.generated_app import GeneratedAppRecord
from benji_api.models.user import OnboardingStatus, OnboardingStep, User


@pytest.mark.anyio
async def test_agent_creates_app_and_public_api_persists_records() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    phone = "+14155552671"
    async with session_factory() as session:
        user = User(
            phone_number=phone,
            onboarding_status=OnboardingStatus.COMPLETE.value,
            onboarding_step=OnboardingStep.COMPLETE.value,
        )
        session.add(user)
        await session.flush()
        conversation = Conversation(user_id=user.id)
        session.add(conversation)
        await session.commit()

    settings = Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        web_chat_dev_identity_enabled=True,
        generated_app_public_url="https://benji.example",
    )
    tool = CreateGeneratedAppTool(settings, session_factory=session_factory)
    result = await tool.execute(
        context=ToolContext(user_id=user.id, conversation_id=conversation.id),
        arguments={
            "template": "budget",
            "title": "Cairo spending",
            "description": "Keep an eye on everyday spending.",
            "theme": "coral",
            "access_mode": "private_link",
            "currency": "EGP",
            "unit": None,
            "target_number": 12_000,
            "target_direction": None,
            "participants": [],
        },
    )
    public_id = result["app_url"].rsplit("/", 1)[-1]
    assert result["app_url"] == f"https://benji.example/apps/{public_id}"

    async def override_session():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            catalog = await client.post("/api/v1/apps/catalog", json={"phone_number": phone})
            assert catalog.status_code == 200
            assert catalog.json()["apps"][0]["title"] == "Cairo spending"

            opened = await client.get(f"/api/v1/apps/public/{public_id}")
            assert opened.status_code == 200
            assert opened.json()["specification"]["settings"] == {
                "currency": "EGP",
                "monthly_budget": 12_000.0,
            }

            added = await client.post(
                f"/api/v1/apps/public/{public_id}/records",
                json={
                    "kind": "expense",
                    "data": {
                        "amount": 250,
                        "category": "food",
                        "note": "lunch",
                        "date": "2026-08-10",
                    },
                },
            )
            assert added.status_code == 200
            record = added.json()["records"][0]
            assert record["data"]["amount"] == 250.0

            invalid = await client.post(
                f"/api/v1/apps/public/{public_id}/records",
                json={
                    "kind": "expense",
                    "data": {
                        "amount": -1,
                        "category": "food",
                        "date": "2026-08-10",
                    },
                },
            )
            assert invalid.status_code == 422

            removed = await client.delete(f"/api/v1/apps/public/{public_id}/records/{record['id']}")
            assert removed.status_code == 200
            assert removed.json()["records"] == []
    finally:
        app.dependency_overrides.clear()
        await engine.dispose()


@pytest.mark.anyio
async def test_group_app_is_always_collaborative() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async with session_factory() as session:
        owner = User(phone_number="+14155552671", display_name="Kareem")
        session.add(owner)
        await session.flush()
        conversation = Conversation(
            user_id=owner.id,
            kind=ConversationKind.GROUP.value,
            title="Cottage",
        )
        session.add(conversation)
        await session.flush()
        session.add_all(
            [
                ConversationMember(
                    conversation_id=conversation.id,
                    user_id=owner.id,
                    external_handle=owner.phone_number,
                    role="owner",
                ),
                ConversationMember(
                    conversation_id=conversation.id,
                    external_handle="+14155552672",
                ),
                ConversationMember(
                    conversation_id=conversation.id,
                    external_handle="friend@example.com",
                ),
            ]
        )
        await session.commit()

    tool = CreateGeneratedAppTool(
        Settings(generated_app_public_url="https://dot.example"),
        session_factory=session_factory,
    )
    result = await tool.execute(
        context=ToolContext(user_id=owner.id, conversation_id=conversation.id),
        arguments={
            "template": "expense_splitter",
            "title": "Cottage",
            "description": "Shared trip expenses.",
            "theme": "ocean",
            "access_mode": "private_link",
            "currency": "CAD",
            "unit": None,
            "target_number": None,
            "target_direction": None,
            "participants": [
                "5a0f59b0-ef77-4e47-8715-0df318dc12f4",
                "+14155552672",
                "friend@example.com",
            ],
        },
    )

    assert result["access_mode"] == "collaborative_link"
    async with session_factory() as session:
        records = (
            await session.scalars(
                select(GeneratedAppRecord).order_by(GeneratedAppRecord.created_at)
            )
        ).all()
    assert [record.data["name"] for record in records] == [
        "Kareem",
        "person 2",
        "person 3",
    ]
    await engine.dispose()
