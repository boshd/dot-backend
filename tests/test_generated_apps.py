import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from benji_api.agents.tools import (
    CreateGeneratedAppRecordTool,
    CreateGeneratedAppTool,
    DeleteGeneratedAppRecordTool,
    DeleteGeneratedAppTool,
    GetGeneratedAppTool,
    ListGeneratedAppsTool,
    UpdateGeneratedAppRecordTool,
)
from benji_api.agents.types import ToolContext
from benji_api.config import Settings, get_settings
from benji_api.db.base import Base
from benji_api.db.session import get_session
from benji_api.main import app
from benji_api.models.channel import Conversation, ConversationKind, ConversationMember
from benji_api.models.generated_app import GeneratedAppRecord
from benji_api.models.user import OnboardingStatus, OnboardingStep, User
from benji_api.services.generated_apps import (
    GeneratedAppNotFoundError,
    create_composable_generated_app,
    create_generated_app,
)


async def _create_legacy_app(
    session_factory,
    *,
    user_id,
    conversation_id,
    arguments,
):
    """Keep legacy runtime coverage independent from the v2 conversation build tool."""

    async with session_factory() as session:
        if "modules" in arguments:
            return await create_composable_generated_app(
                session,
                user_id=user_id,
                conversation_id=conversation_id,
                title=arguments["title"],
                description=arguments["description"],
                theme=arguments["theme"],
                access_mode=arguments["access_mode"],
                modules=arguments["modules"],
                initial_records=arguments["initial_records"],
            )
        return await create_generated_app(
            session,
            user_id=user_id,
            conversation_id=conversation_id,
            title=arguments["title"],
            description=arguments["description"],
            template=arguments["template"],
            theme=arguments["theme"],
            access_mode=arguments["access_mode"],
            currency=arguments["currency"],
            unit=arguments["unit"],
            target_number=arguments["target_number"],
            target_direction=arguments["target_direction"],
            participants=arguments["participants"],
        )


def test_get_generated_app_tool_schema_declares_record_controls() -> None:
    definition = GetGeneratedAppTool(Settings()).definition

    assert set(definition.parameters["properties"]) == {
        "app_id",
        "record_kind",
        "record_limit",
    }
    assert set(definition.parameters["required"]) == {
        "app_id",
        "record_kind",
        "record_limit",
    }


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
    bundle = await _create_legacy_app(
        session_factory,
        user_id=user.id,
        conversation_id=conversation.id,
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
    result = {
        "app_id": str(bundle.app.id),
        "app_url": f"https://benji.example/apps/{bundle.app.public_id}",
    }
    public_id = bundle.app.public_id

    context = ToolContext(user_id=user.id, conversation_id=conversation.id)
    inspected = await GetGeneratedAppTool(
        settings,
        session_factory=session_factory,
    ).execute(
        context=context,
        arguments={"app_id": result["app_id"], "record_kind": None, "record_limit": 100},
    )
    assert inspected["app_id"] == result["app_id"]
    assert inspected["records"] == []

    async with session_factory() as session:
        other_user = User(phone_number="+14155552672")
        session.add(other_user)
        await session.flush()
        other_conversation = Conversation(user_id=other_user.id)
        session.add(other_conversation)
        await session.commit()
    with pytest.raises(GeneratedAppNotFoundError):
        await GetGeneratedAppTool(
            settings,
            session_factory=session_factory,
        ).execute(
            context=ToolContext(
                user_id=other_user.id,
                conversation_id=other_conversation.id,
            ),
            arguments={"app_id": result["app_id"], "record_kind": None, "record_limit": 100},
        )

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

            listed = await ListGeneratedAppsTool(
                settings,
                session_factory=session_factory,
            ).execute(
                context=ToolContext(user_id=user.id, conversation_id=conversation.id),
                arguments={},
            )
            assert listed["count"] == 1
            assert listed["apps"][0]["title"] == "Cairo spending"
            assert listed["apps"][0]["app_url"] == result["app_url"]

            deleted = await DeleteGeneratedAppTool(
                session_factory=session_factory,
            ).execute(
                context=ToolContext(user_id=user.id, conversation_id=conversation.id),
                arguments={"app_id": result["app_id"]},
            )
            assert deleted == {
                "app_id": result["app_id"],
                "deleted": True,
                "message_hint": (
                    "The app and its public link are disabled. Do not share the old link as active."
                ),
            }
            assert (await client.get(f"/api/v1/apps/public/{public_id}")).status_code == 404
    finally:
        app.dependency_overrides.clear()
        await engine.dispose()


@pytest.mark.anyio
async def test_agent_manages_owned_app_records_with_existing_validation() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with session_factory() as session:
        user = User(phone_number="+14155552671", display_name="Kareem")
        session.add(user)
        await session.flush()
        conversation = Conversation(user_id=user.id)
        session.add(conversation)
        await session.commit()

    settings = Settings(generated_app_public_url="https://dot.example")
    context = ToolContext(user_id=user.id, conversation_id=conversation.id)
    app_bundle = await _create_legacy_app(
        session_factory,
        user_id=user.id,
        conversation_id=conversation.id,
        arguments={
            "title": "Cairo spending",
            "description": "Daily expenses",
            "theme": "coral",
            "access_mode": "private_link",
            "modules": [
                {
                    "id": "expenses",
                    "type": "expenses",
                    "title": "Expenses",
                    "description": "What I spent",
                    "settings": {"currency": "EGP", "budget": 10_000, "mode": "personal"},
                }
            ],
            "initial_records": [],
        },
    )
    app_result = {"app_id": str(app_bundle.app.id)}
    record = {
        "module_id": "expenses",
        "kind": "expense",
        "actor_name": None,
        "data": {
            "amount": 125,
            "category": "food",
            "note": "lunch",
            "date": "2026-08-10",
            "paid_by": None,
            "split_between": [],
        },
    }
    created = await CreateGeneratedAppRecordTool(
        settings,
        session_factory=session_factory,
    ).execute(
        context=context,
        arguments={"app_id": app_result["app_id"], "record": record},
    )
    record_id = created["created_record_id"]
    assert created["records"][0]["actor_name"] == "Kareem"
    assert created["records"][0]["data"]["amount"] == 125.0

    inspected = await GetGeneratedAppTool(
        settings,
        session_factory=session_factory,
    ).execute(
        context=context,
        arguments={
            "app_id": app_result["app_id"],
            "record_kind": "expense",
            "record_limit": 10,
        },
    )
    assert inspected["record_count"] == 1
    assert inspected["records"][0]["record_id"] == record_id

    record["data"] = {**record["data"], "amount": 150, "note": "lunch and coffee"}
    updated = await UpdateGeneratedAppRecordTool(
        settings,
        session_factory=session_factory,
    ).execute(
        context=context,
        arguments={"app_id": app_result["app_id"], "record_id": record_id, "record": record},
    )
    assert updated["updated_record_id"] == record_id
    assert updated["records"][0]["data"]["amount"] == 150.0

    invalid_record = {**record, "data": {**record["data"], "amount": -1}}
    with pytest.raises(ValueError, match="amount"):
        await CreateGeneratedAppRecordTool(
            settings,
            session_factory=session_factory,
        ).execute(
            context=context,
            arguments={"app_id": app_result["app_id"], "record": invalid_record},
        )

    deleted = await DeleteGeneratedAppRecordTool(
        settings,
        session_factory=session_factory,
    ).execute(
        context=context,
        arguments={"app_id": app_result["app_id"], "record_id": record_id},
    )
    assert deleted["deleted_record_id"] == record_id
    assert deleted["records"] == []
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
                    display_name="Alex",
                ),
                ConversationMember(
                    conversation_id=conversation.id,
                    external_handle="friend@example.com",
                    display_name="Alex",
                ),
            ]
        )
        await session.commit()

    result_bundle = await _create_legacy_app(
        session_factory,
        user_id=owner.id,
        conversation_id=conversation.id,
        arguments={
            "title": "Cottage",
            "description": "Shared trip expenses.",
            "theme": "ocean",
            "access_mode": "private_link",
            "modules": [
                {
                    "id": "expenses",
                    "type": "expenses",
                    "title": "Expenses",
                    "description": "Track the cottage costs.",
                    "settings": {"currency": "CAD", "budget": None, "mode": "split"},
                },
                {
                    "id": "guests",
                    "type": "guest_list",
                    "title": "Guests",
                    "description": "Who's coming.",
                    "settings": {"allow_plus_ones": False},
                },
            ],
            "initial_records": [
                *[
                    {
                        "module_id": "expenses",
                        "kind": "participant",
                        "actor_name": None,
                        "data": {"name": handle},
                    }
                    for handle in [
                        "5a0f59b0-ef77-4e47-8715-0df318dc12f4",
                        "+14155552672",
                        "friend@example.com",
                    ]
                ],
                {
                    "module_id": "expenses",
                    "kind": "expense",
                    "actor_name": "+14155552672",
                    "data": {
                        "amount": 90,
                        "category": "food",
                        "note": "groceries",
                        "date": "2026-08-11",
                        "paid_by": "+14155552672",
                        "split_between": ["everyone"],
                    },
                },
                {
                    "module_id": "guests",
                    "kind": "guest",
                    "actor_name": None,
                    "data": {
                        "name": "everyone",
                        "status": "invited",
                        "party_size": 1,
                        "note": "",
                    },
                },
            ],
        },
    )
    assert result_bundle.app.access_mode == "collaborative_link"
    async with session_factory() as session:
        records = (
            await session.scalars(
                select(GeneratedAppRecord).order_by(GeneratedAppRecord.created_at)
            )
        ).all()
    participants = [record for record in records if record.kind == "participant"]
    guests = [record for record in records if record.kind == "guest"]
    expense = next(record for record in records if record.kind == "expense")
    assert [record.data["name"] for record in participants] == [
        "Kareem",
        "Alex",
        "Alex 2",
    ]
    assert [record.data["name"] for record in guests] == ["Kareem", "Alex", "Alex 2"]
    assert expense.data["paid_by"] == "Alex"
    assert expense.data["split_between"] == ["Kareem", "Alex", "Alex 2"]
    assert expense.actor_name == "Alex"

    safe_bundle = await _create_legacy_app(
        session_factory,
        user_id=owner.id,
        conversation_id=conversation.id,
        arguments={
            "title": "Second cottage split",
            "description": "Uses known display names with handle-based expense references.",
            "theme": "sage",
            "access_mode": "private_link",
            "modules": [
                {
                    "id": "expenses",
                    "type": "expenses",
                    "title": "Expenses",
                    "description": "",
                    "settings": {"currency": "CAD", "budget": None, "mode": "split"},
                }
            ],
            "initial_records": [
                *[
                    {
                        "module_id": "expenses",
                        "kind": "participant",
                        "actor_name": None,
                        "data": {"name": name},
                    }
                    for name in ["Kareem", "Alex", "Alex 2"]
                ],
                {
                    "module_id": "expenses",
                    "kind": "expense",
                    "actor_name": "friend@example.com",
                    "data": {
                        "amount": 45,
                        "category": "transport",
                        "note": "taxi",
                        "date": "2026-08-11",
                        "paid_by": "friend@example.com",
                        "split_between": ["everyone"],
                    },
                },
            ],
        },
    )
    async with session_factory() as session:
        safe_records = (
            await session.scalars(
                select(GeneratedAppRecord)
                .where(GeneratedAppRecord.app_id == safe_bundle.app.id)
                .order_by(GeneratedAppRecord.created_at)
            )
        ).all()
    safe_expense = next(record for record in safe_records if record.kind == "expense")
    assert safe_expense.data["paid_by"] == "Alex 2"
    assert safe_expense.data["split_between"] == ["Kareem", "Alex", "Alex 2"]
    assert safe_expense.actor_name == "Alex 2"
    await engine.dispose()


@pytest.mark.anyio
async def test_birthday_request_creates_one_seeded_multi_module_app() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    phone = "+14155552673"
    async with session_factory() as session:
        user = User(phone_number=phone)
        session.add(user)
        await session.flush()
        conversation = Conversation(user_id=user.id)
        session.add(conversation)
        await session.commit()

    settings = Settings(
        web_chat_dev_identity_enabled=True,
        generated_app_public_url="https://dot.example",
    )
    bundle = await _create_legacy_app(
        session_factory,
        user_id=user.id,
        conversation_id=conversation.id,
        arguments={
            "title": "Nour's birthday",
            "description": "Plan the party in one place.",
            "theme": "gold",
            "access_mode": "collaborative_link",
            "modules": [
                {
                    "id": "overview",
                    "type": "overview",
                    "title": "Birthday",
                    "description": "The plan at a glance.",
                    "settings": {
                        "body": "Everything for Nour's birthday in one place.",
                        "facts": [
                            {"label": "Date", "value": "August 20, 2026"},
                            {"label": "Location", "value": "Home"},
                        ],
                    },
                },
                {
                    "id": "todos",
                    "type": "todos",
                    "title": "To dos",
                    "description": "Everything to get done.",
                    "settings": {"show_completed": True},
                },
                {
                    "id": "guests",
                    "type": "guest_list",
                    "title": "Guests",
                    "description": "Invites and RSVPs.",
                    "settings": {"allow_plus_ones": True},
                },
                {
                    "id": "plan",
                    "type": "itinerary",
                    "title": "Plan",
                    "description": "The party timeline.",
                    "settings": {"timezone": "Africa/Cairo"},
                },
            ],
            "initial_records": [
                {
                    "module_id": "todos",
                    "kind": "todo",
                    "actor_name": None,
                    "data": {
                        "text": "Book the cake",
                        "completed": False,
                        "due_date": None,
                        "assignee": None,
                        "priority": "high",
                    },
                },
                {
                    "module_id": "guests",
                    "kind": "guest",
                    "actor_name": None,
                    "data": {
                        "name": "Nour",
                        "status": "going",
                        "party_size": 1,
                        "note": "birthday person",
                    },
                },
                {
                    "module_id": "plan",
                    "kind": "itinerary_item",
                    "actor_name": None,
                    "data": {
                        "title": "Cake and candles",
                        "date": "2026-08-20",
                        "start_time": "20:00",
                        "end_time": "20:30",
                        "location": "home",
                        "note": "",
                        "completed": False,
                    },
                },
            ],
        },
    )
    assert bundle.app.template == "workspace"
    assert [
        {"id": module["id"], "type": module["type"]}
        for module in bundle.version.specification["modules"]
    ] == [
        {"id": "overview", "type": "overview"},
        {"id": "todos", "type": "todos"},
        {"id": "guests", "type": "guest_list"},
        {"id": "plan", "type": "itinerary"},
    ]
    public_id = bundle.app.public_id

    async def override_session():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            opened = await client.get(f"/api/v1/apps/public/{public_id}")
            assert opened.status_code == 200
            payload = opened.json()
            assert payload["specification"]["schema_version"] == 2
            assert [module["type"] for module in payload["specification"]["modules"]] == [
                "overview",
                "todos",
                "guest_list",
                "itinerary",
            ]
            assert payload["specification"]["modules"][0]["settings"]["facts"][0] == {
                "label": "Date",
                "value": "August 20, 2026",
            }
            assert {record["module_id"] for record in payload["records"]} == {
                "todos",
                "guests",
                "plan",
            }

            todo = next(record for record in payload["records"] if record["kind"] == "todo")
            updated = await client.patch(
                f"/api/v1/apps/public/{public_id}/records/{todo['id']}",
                json={"data": {"completed": True}},
            )
            assert updated.status_code == 200
            changed = next(
                record for record in updated.json()["records"] if record["id"] == todo["id"]
            )
            assert changed["data"]["completed"] is True

            wrong_module = await client.post(
                f"/api/v1/apps/public/{public_id}/records",
                json={
                    "module_id": "todos",
                    "kind": "guest",
                    "data": {
                        "name": "Omar",
                        "status": "invited",
                        "party_size": 1,
                        "note": "",
                    },
                },
            )
            assert wrong_module.status_code == 422

            unknown_module = await client.post(
                f"/api/v1/apps/public/{public_id}/records",
                json={
                    "module_id": "missing",
                    "kind": "guest",
                    "data": {
                        "name": "Omar",
                        "status": "invited",
                        "party_size": 1,
                        "note": "",
                    },
                },
            )
            assert unknown_module.status_code == 422
    finally:
        app.dependency_overrides.clear()
        await engine.dispose()


@pytest.mark.anyio
async def test_collection_module_crud_validates_configured_fields() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    phone = "+14155552674"
    async with session_factory() as session:
        user = User(phone_number=phone)
        session.add(user)
        await session.flush()
        conversation = Conversation(user_id=user.id)
        session.add(conversation)
        await session.commit()

    settings = Settings(web_chat_dev_identity_enabled=True)
    bundle = await _create_legacy_app(
        session_factory,
        user_id=user.id,
        conversation_id=conversation.id,
        arguments={
            "title": "Venue shortlist",
            "description": "Compare possible venues.",
            "theme": "sage",
            "access_mode": "private_link",
            "modules": [
                {
                    "id": "venues",
                    "type": "collection",
                    "title": "Venues",
                    "description": "The shortlist.",
                    "settings": {
                        "display": "cards",
                        "primary_field": "name",
                        "currency": "EGP",
                        "fields": [
                            {
                                "key": "name",
                                "label": "Name",
                                "type": "text",
                                "required": True,
                                "options": [],
                            },
                            {
                                "key": "status",
                                "label": "Status",
                                "type": "select",
                                "required": True,
                                "options": ["maybe", "booked"],
                            },
                            {
                                "key": "price",
                                "label": "Price",
                                "type": "currency",
                                "required": False,
                                "options": [],
                            },
                        ],
                    },
                }
            ],
            "initial_records": [
                {
                    "module_id": "venues",
                    "kind": "entry",
                    "actor_name": None,
                    "data": {
                        "name": "The Garden",
                        "status": "maybe",
                        "price": 12_000,
                    },
                }
            ],
        },
    )
    public_id = bundle.app.public_id

    async def override_session():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            opened = await client.get(f"/api/v1/apps/public/{public_id}")
            assert opened.json()["specification"]["modules"][0]["settings"]["currency"] == "EGP"
            record = opened.json()["records"][0]
            assert record["module_id"] == "venues"
            assert record["data"] == {
                "name": "The Garden",
                "status": "maybe",
                "price": 12_000.0,
            }

            updated = await client.patch(
                f"/api/v1/apps/public/{public_id}/records/{record['id']}",
                json={"data": {"status": "booked", "price": None}},
            )
            assert updated.status_code == 200
            assert updated.json()["records"][0]["data"]["price"] is None

            invalid = await client.post(
                f"/api/v1/apps/public/{public_id}/records",
                json={
                    "module_id": "venues",
                    "kind": "entry",
                    "data": {"name": "Other", "status": "not-an-option"},
                },
            )
            assert invalid.status_code == 422

            deleted = await client.delete(f"/api/v1/apps/public/{public_id}/records/{record['id']}")
            assert deleted.status_code == 200
            assert deleted.json()["records"] == []
    finally:
        app.dependency_overrides.clear()
        await engine.dispose()


@pytest.mark.anyio
async def test_split_participant_cannot_be_renamed_or_deleted_while_referenced() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with session_factory() as session:
        user = User(phone_number="+14155552677")
        session.add(user)
        await session.flush()
        conversation = Conversation(user_id=user.id)
        session.add(conversation)
        await session.commit()

    settings = Settings(web_chat_dev_identity_enabled=True)
    bundle = await _create_legacy_app(
        session_factory,
        user_id=user.id,
        conversation_id=conversation.id,
        arguments={
            "title": "Trip split",
            "description": "Shared expenses.",
            "theme": "ocean",
            "access_mode": "collaborative_link",
            "modules": [
                {
                    "id": "expenses",
                    "type": "expenses",
                    "title": "Expenses",
                    "description": "",
                    "settings": {"currency": "CAD", "budget": None, "mode": "split"},
                }
            ],
            "initial_records": [
                {
                    "module_id": "expenses",
                    "kind": "participant",
                    "actor_name": None,
                    "data": {"name": "Alice"},
                },
                {
                    "module_id": "expenses",
                    "kind": "participant",
                    "actor_name": None,
                    "data": {"name": "Bob"},
                },
                {
                    "module_id": "expenses",
                    "kind": "expense",
                    "actor_name": "Alice",
                    "data": {
                        "amount": 40,
                        "category": "food",
                        "note": "dinner",
                        "date": "2026-08-11",
                        "paid_by": "Alice",
                        "split_between": ["Alice", "Bob"],
                    },
                },
            ],
        },
    )
    public_id = bundle.app.public_id

    async def override_session():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            opened = await client.get(f"/api/v1/apps/public/{public_id}")
            alice = next(
                record
                for record in opened.json()["records"]
                if record["kind"] == "participant" and record["data"]["name"] == "Alice"
            )

            renamed = await client.patch(
                f"/api/v1/apps/public/{public_id}/records/{alice['id']}",
                json={"data": {"name": "Alicia"}},
            )
            assert renamed.status_code == 422

            deleted = await client.delete(f"/api/v1/apps/public/{public_id}/records/{alice['id']}")
            assert deleted.status_code == 422

            unchanged = await client.get(f"/api/v1/apps/public/{public_id}")
            assert any(
                record["kind"] == "participant" and record["data"]["name"] == "Alice"
                for record in unchanged.json()["records"]
            )
    finally:
        app.dependency_overrides.clear()
        await engine.dispose()


@pytest.mark.anyio
async def test_legacy_v1_checklist_item_remains_editable_without_module_id() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    phone = "+14155552675"
    async with session_factory() as session:
        user = User(phone_number=phone)
        session.add(user)
        await session.flush()
        conversation = Conversation(user_id=user.id)
        session.add(conversation)
        await session.flush()
        bundle = await create_generated_app(
            session,
            user_id=user.id,
            conversation_id=conversation.id,
            title="Old checklist",
            description="",
            template="checklist",
            theme="coral",
            access_mode="private_link",
            currency=None,
            unit=None,
            target_number=None,
            target_direction=None,
            participants=[],
        )
        bundle.version.specification = {
            "schema_version": 1,
            "template": "checklist",
            "theme": "coral",
            "settings": {},
            "capabilities": ["items", "completion_progress"],
        }
        await session.commit()
        public_id = bundle.app.public_id

    settings = Settings(web_chat_dev_identity_enabled=True)

    async def override_session():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            added = await client.post(
                f"/api/v1/apps/public/{public_id}/records",
                json={"kind": "item", "data": {"text": "legacy", "completed": False}},
            )
            assert added.status_code == 200
            item = added.json()["records"][0]
            assert item["module_id"] == "todos"

            toggled = await client.patch(
                f"/api/v1/apps/public/{public_id}/records/{item['id']}",
                json={"data": {"completed": True}},
            )
            assert toggled.status_code == 200
            assert toggled.json()["records"][0]["data"]["completed"] is True
    finally:
        app.dependency_overrides.clear()
        await engine.dispose()


@pytest.mark.anyio
async def test_legacy_expense_splitter_keeps_participant_references() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with session_factory() as session:
        user = User(phone_number="+14155552676")
        session.add(user)
        await session.flush()
        conversation = Conversation(user_id=user.id)
        session.add(conversation)
        await session.flush()
        bundle = await create_generated_app(
            session,
            user_id=user.id,
            conversation_id=conversation.id,
            title="Trip split",
            description="",
            template="expense_splitter",
            theme="ocean",
            access_mode="collaborative_link",
            currency="CAD",
            unit=None,
            target_number=None,
            target_direction=None,
            participants=["Alice", "Bob"],
        )
        public_id = bundle.app.public_id

    settings = Settings(web_chat_dev_identity_enabled=True)

    async def override_session():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            added = await client.post(
                f"/api/v1/apps/public/{public_id}/records",
                json={
                    "kind": "expense",
                    "data": {
                        "amount": 100,
                        "description": "Dinner",
                        "paid_by": "Alice",
                        "split_between": ["Alice", "Bob"],
                        "date": "2026-08-20",
                    },
                },
            )
            assert added.status_code == 200
            expense = next(
                record for record in added.json()["records"] if record["kind"] == "expense"
            )
            alice = next(
                record
                for record in added.json()["records"]
                if record["kind"] == "participant" and record["data"]["name"] == "Alice"
            )
            assert expense["module_id"] == "expenses"

            blocked = await client.delete(f"/api/v1/apps/public/{public_id}/records/{alice['id']}")
            assert blocked.status_code == 422

            assert (
                await client.delete(f"/api/v1/apps/public/{public_id}/records/{expense['id']}")
            ).status_code == 200
            assert (
                await client.delete(f"/api/v1/apps/public/{public_id}/records/{alice['id']}")
            ).status_code == 200
    finally:
        app.dependency_overrides.clear()
        await engine.dispose()


def test_create_app_tool_schema_closes_every_object_for_strict_mode() -> None:
    schema = CreateGeneratedAppTool(Settings()).definition.parameters

    def assert_closed(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node.get("additionalProperties") is False
                assert set(node.get("required", [])) == set(node.get("properties", {}))
            for value in node.values():
                assert_closed(value)
        elif isinstance(node, list):
            for value in node:
                assert_closed(value)

    assert_closed(schema)
