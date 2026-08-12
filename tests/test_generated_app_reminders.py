from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from generated_app_artifacts import compiled_artifact
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from benji_api.agents.tools import CreateGeneratedAppTool
from benji_api.agents.types import ToolContext
from benji_api.api.dependencies import get_optional_authenticated_user
from benji_api.config import Settings
from benji_api.db.base import Base
from benji_api.db.session import get_session
from benji_api.generated_app_contract import DOT_REMINDER_CREATE_CAPABILITY
from benji_api.main import app as api
from benji_api.models.channel import Conversation, ConversationKind, ConversationMember
from benji_api.models.generated_app_v2 import (
    GeneratedAppBuildJob,
    GeneratedAppEvent,
    GeneratedAppMembership,
    GeneratedAppRole,
)
from benji_api.models.schedule import ScheduledTask
from benji_api.models.user import User
from benji_api.services.generated_apps_v2 import (
    CodeAppValidationError,
    claim_next_build,
    complete_build,
    create_code_app_build,
)


def _manifest(*, reminders: bool) -> dict[str, object]:
    return {
        "schema_version": 1,
        "capabilities": [DOT_REMINDER_CREATE_CAPABILITY] if reminders else [],
        "entities": [],
    }


async def _database():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, factory


async def _deployed_app(factory, *, reminders: bool, group: bool = False):
    manifest = _manifest(reminders=reminders)
    async with factory() as session:
        owner = User(phone_number="+14155550901")
        session.add(owner)
        await session.flush()
        conversation = Conversation(
            user_id=owner.id,
            kind=ConversationKind.GROUP.value if group else ConversationKind.DIRECT.value,
        )
        session.add(conversation)
        await session.flush()
        if group:
            session.add(
                ConversationMember(
                    conversation_id=conversation.id,
                    user_id=owner.id,
                    external_handle=owner.phone_number,
                    role="owner",
                )
            )
        await session.commit()
        app, _, ticket = await create_code_app_build(
            session,
            user_id=owner.id,
            conversation_id=conversation.id,
            title="Gentle habits",
            description="A habit tracker that can remind me",
            request={
                "blueprint": {
                    "title": "Gentle habits",
                    "description": "A habit tracker that can remind me",
                    "purpose": "Keep a habit alive",
                    "manifest": manifest,
                    "seed_data": {},
                }
            },
        )
    async with factory() as session:
        claim = await claim_next_build(session, worker_id="reminder-test")
        assert claim is not None
        await complete_build(
            session,
            job_id=claim.job_id,
            worker_id="reminder-test",
            expected_attempt=claim.attempt,
            manifest=manifest,
            source_files={"src/App.tsx": "export default function App() { return null }"},
            artifact=compiled_artifact(sdk_version="1"),
            artifact_url="artifact://gentle-habits",
            artifact_sha256="a" * 64,
            sdk_version="1",
        )
    return owner, conversation, app, ticket


def _reminder_request(*, key: str = "reminder-action-0001") -> dict[str, object]:
    return {
        "operation": DOT_REMINDER_CREATE_CAPABILITY,
        "idempotency_key": key,
        "args": {
            "title": "evening walk",
            "goal": "remind me to take my evening walk",
            "run_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
            "timezone": "Africa/Cairo",
            "recurrence": "weekly",
        },
    }


@pytest.mark.anyio
async def test_declared_reminder_creates_one_idempotent_general_schedule() -> None:
    engine, factory = await _database()
    owner, conversation, app, _ = await _deployed_app(factory, reminders=True)

    async def override_session():
        async with factory() as session:
            yield session

    api.dependency_overrides[get_session] = override_session
    api.dependency_overrides[get_optional_authenticated_user] = lambda: owner
    try:
        async with AsyncClient(transport=ASGITransport(app=api), base_url="http://test") as client:
            opened = await client.get(f"/api/v1/apps/v2/{app.public_id}")
            assert opened.json()["access"]["capabilities"] == [
                DOT_REMINDER_CREATE_CAPABILITY
            ]
            reminder_request = _reminder_request()
            first = await client.post(
                f"/api/v1/apps/v2/{app.id}/actions", json=reminder_request
            )
            assert first.status_code == 200
            repeated = await client.post(
                f"/api/v1/apps/v2/{app.id}/actions", json=reminder_request
            )
            assert repeated.status_code == 200
            assert repeated.json()["data"]["schedule_id"] == first.json()["data"]["schedule_id"]
            changed_replay = {
                **reminder_request,
                "args": {
                    **reminder_request["args"],
                    "goal": "this is a different reminder request",
                },
            }
            conflict = await client.post(
                f"/api/v1/apps/v2/{app.id}/actions", json=changed_replay
            )
            assert conflict.status_code == 409
            invalid = _reminder_request(key="invalid-reminder-0001")
            invalid["args"] = {
                **invalid["args"],
                "run_at": "2026-08-13T18:00:00",
                "message": "arbitrary proactive message",
            }
            rejected = await client.post(
                f"/api/v1/apps/v2/{app.id}/actions", json=invalid
            )
            assert rejected.status_code == 422
    finally:
        api.dependency_overrides.clear()

    async with factory() as session:
        tasks = list((await session.scalars(select(ScheduledTask))).all())
        events = list((await session.scalars(select(GeneratedAppEvent))).all())
        assert len(tasks) == 1
        assert tasks[0].action_type == "agent.reachout"
        assert tasks[0].source == "generated_app"
        assert tasks[0].conversation_id == conversation.id
        assert tasks[0].payload["generated_app_id"] == str(app.id)
        assert tasks[0].payload["tool_policy"] == "message_only"
        assert [event.event_type for event in events] == ["app.reminder.created"]
        assert events[0].actor_user_id == owner.id
        assert events[0].operation == DOT_REMINDER_CREATE_CAPABILITY
        assert len(events[0].request_hash or "") == 64
        assert events[0].response["schedule_id"] == str(tasks[0].id)
    await engine.dispose()


@pytest.mark.anyio
async def test_reminder_requires_manifest_grant_and_non_anonymous_owner_or_editor() -> None:
    engine, factory = await _database()
    owner, _, app, _ = await _deployed_app(factory, reminders=False)

    async def override_session():
        async with factory() as session:
            yield session

    api.dependency_overrides[get_session] = override_session
    api.dependency_overrides[get_optional_authenticated_user] = lambda: owner
    try:
        async with AsyncClient(transport=ASGITransport(app=api), base_url="http://test") as client:
            denied = await client.post(
                f"/api/v1/apps/v2/{app.id}/actions", json=_reminder_request()
            )
            assert denied.status_code == 403
    finally:
        api.dependency_overrides.clear()

    await engine.dispose()


@pytest.mark.anyio
async def test_owner_bound_handoff_is_not_identity_proof_for_reminders() -> None:
    engine, factory = await _database()
    _, _, app, owner_ticket = await _deployed_app(factory, reminders=True)

    async def override_session():
        async with factory() as session:
            yield session

    api.dependency_overrides[get_session] = override_session
    api.dependency_overrides[get_optional_authenticated_user] = lambda: None
    try:
        async with AsyncClient(transport=ASGITransport(app=api), base_url="http://test") as client:
            redeemed = await client.post(
                f"/api/v1/apps/v2/{app.public_id}/sessions/redeem",
                json={"ticket": owner_ticket},
            )
            token = redeemed.json()["session_token"]
            headers = {"X-Dot-App-Session": token}
            opened = await client.get(
                f"/api/v1/apps/v2/{app.public_id}",
                headers=headers,
            )
            assert opened.json()["access"]["role"] == "owner"
            assert opened.json()["access"]["capabilities"] == []
            denied = await client.post(
                f"/api/v1/apps/v2/{app.id}/actions",
                headers=headers,
                json=_reminder_request(),
            )
            assert denied.status_code == 403
            assert "authenticated" in denied.json()["detail"]
    finally:
        api.dependency_overrides.clear()
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(ScheduledTask)) == 0
    await engine.dispose()
    engine, factory = await _database()
    _, _, shared_app, group_ticket = await _deployed_app(
        factory, reminders=True, group=True
    )

    async def override_group_session():
        async with factory() as session:
            yield session

    api.dependency_overrides[get_session] = override_group_session
    api.dependency_overrides[get_optional_authenticated_user] = lambda: None
    try:
        async with AsyncClient(transport=ASGITransport(app=api), base_url="http://test") as client:
            redeemed = await client.post(
                f"/api/v1/apps/v2/{shared_app.public_id}/sessions/redeem",
                json={"ticket": group_ticket},
            )
            token = redeemed.json()["session_token"]
            opened = await client.get(
                f"/api/v1/apps/v2/{shared_app.public_id}",
                headers={"X-Dot-App-Session": token},
            )
            assert opened.json()["access"]["capabilities"] == []
            denied = await client.post(
                f"/api/v1/apps/v2/{shared_app.id}/actions",
                headers={"X-Dot-App-Session": token},
                json=_reminder_request(),
            )
            assert denied.status_code == 403
            assert "owner or editor" in denied.json()["detail"]
    finally:
        api.dependency_overrides.clear()
        await engine.dispose()


@pytest.mark.anyio
async def test_verified_editor_wins_over_handoff_and_outsider_falls_back_to_session() -> None:
    engine, factory = await _database()
    owner, _, app, owner_ticket = await _deployed_app(factory, reminders=True)
    async with factory() as session:
        editor = User(phone_number="+14155550911")
        outsider = User(phone_number="+14155550912")
        session.add_all([editor, outsider])
        await session.flush()
        session.add(Conversation(user_id=editor.id))
        session.add(
            GeneratedAppMembership(
                app_id=app.id,
                user_id=editor.id,
                role=GeneratedAppRole.EDITOR.value,
            )
        )
        await session.commit()

    async def override_session():
        async with factory() as session:
            yield session

    current_user: dict[str, User | None] = {"value": outsider}
    api.dependency_overrides[get_session] = override_session
    api.dependency_overrides[get_optional_authenticated_user] = lambda: current_user["value"]
    try:
        async with AsyncClient(transport=ASGITransport(app=api), base_url="http://test") as client:
            redeemed = await client.post(
                f"/api/v1/apps/v2/{app.public_id}/sessions/redeem",
                json={"ticket": owner_ticket},
            )
            token = redeemed.json()["session_token"]
            headers = {"X-Dot-App-Session": token}

            fallback = await client.get(
                f"/api/v1/apps/v2/{app.public_id}",
                headers=headers,
            )
            assert fallback.status_code == 200
            assert fallback.json()["access"]["role"] == GeneratedAppRole.OWNER.value
            assert fallback.json()["access"]["capabilities"] == []

            current_user["value"] = editor
            verified = await client.get(
                f"/api/v1/apps/v2/{app.public_id}",
                headers=headers,
            )
            assert verified.status_code == 200
            assert verified.json()["access"]["role"] == GeneratedAppRole.EDITOR.value
            assert verified.json()["access"]["capabilities"] == [
                DOT_REMINDER_CREATE_CAPABILITY
            ]
            created = await client.post(
                f"/api/v1/apps/v2/{app.id}/actions",
                headers=headers,
                json=_reminder_request(key="verified-editor-reminder"),
            )
            assert created.status_code == 200
    finally:
        api.dependency_overrides.clear()

    async with factory() as session:
        task = await session.scalar(select(ScheduledTask))
        assert task is not None
        assert task.user_id == editor.id
        assert task.user_id != owner.id
    await engine.dispose()


@pytest.mark.anyio
async def test_durable_per_actor_mutation_limit_returns_429() -> None:
    engine, factory = await _database()
    owner, _, app, _ = await _deployed_app(factory, reminders=True)
    async with factory() as session:
        session.add_all(
            [
                GeneratedAppEvent(
                    app_id=app.id,
                    event_type="app.data.created",
                    actor_user_id=owner.id,
                    idempotency_key=f"seed-rate-limit-{index:03d}",
                    payload={},
                )
                for index in range(120)
            ]
        )
        await session.commit()

    async def override_session():
        async with factory() as session:
            yield session

    api.dependency_overrides[get_session] = override_session
    api.dependency_overrides[get_optional_authenticated_user] = lambda: owner
    try:
        async with AsyncClient(transport=ASGITransport(app=api), base_url="http://test") as client:
            response = await client.post(
                f"/api/v1/apps/v2/{app.id}/actions",
                json=_reminder_request(key="rate-limited-reminder"),
            )
            assert response.status_code == 429
            assert "too much" in response.json()["detail"]
    finally:
        api.dependency_overrides.clear()
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(ScheduledTask)) == 0
    await engine.dispose()


@pytest.mark.anyio
async def test_create_tool_carries_only_the_explicit_reminder_grant() -> None:
    engine, factory = await _database()
    async with factory() as session:
        user = User(phone_number="+14155550902")
        session.add(user)
        await session.flush()
        conversation = Conversation(user_id=user.id)
        session.add(conversation)
        await session.commit()
    tool = CreateGeneratedAppTool(
        Settings(generated_app_public_url="https://app.textdot.test"),
        session_factory=factory,
    )
    assert "explicitly asked" in tool.definition.parameters["properties"]["capabilities"][
        "description"
    ]
    assert "uniqueItems" not in tool.definition.parameters["properties"]["capabilities"]
    result = await tool.execute(
        context=ToolContext(
            user_id=user.id,
            conversation_id=conversation.id,
            delivery_provider=None,
        ),
        arguments={
            "title": "Water tracker",
            "description": "Track water and remind me to drink",
            "purpose": "Drink enough water",
            "product_brief": "A simple water log with a user-triggered reminder control.",
            "visual_direction": "Calm, fluid, and focused.",
            "access_mode": "private_link",
            "entities": [],
            "capabilities": [DOT_REMINDER_CREATE_CAPABILITY],
            "seed_data": "{}",
        },
    )
    async with factory() as session:
        job = await session.get(GeneratedAppBuildJob, UUID(result["build_job_id"]))
        assert job is not None
        assert job.delivery_provider is None
        assert job.request["blueprint"]["manifest"]["capabilities"] == [
            DOT_REMINDER_CREATE_CAPABILITY
        ]
    await engine.dispose()


@pytest.mark.anyio
async def test_unsupported_manifest_capability_never_deploys() -> None:
    engine, factory = await _database()
    async with factory() as session:
        user = User(phone_number="+14155550903")
        session.add(user)
        await session.flush()
        conversation = Conversation(user_id=user.id)
        session.add(conversation)
        await session.commit()
        _, _, _ = await create_code_app_build(
            session,
            user_id=user.id,
            conversation_id=conversation.id,
            title="Unsafe app",
            description="",
            request={"prompt": "try unsupported authority"},
        )
    async with factory() as session:
        claim = await claim_next_build(session, worker_id="bad-capability")
        assert claim is not None
        with pytest.raises(CodeAppValidationError, match="unsupported capability"):
            await complete_build(
                session,
                job_id=claim.job_id,
                worker_id="bad-capability",
                expected_attempt=claim.attempt,
                manifest={
                    "schema_version": 1,
                    "capabilities": ["dot.message.send"],
                    "entities": [],
                },
                source_files={"src/App.tsx": "export default function App() { return null }"},
                artifact=compiled_artifact(sdk_version="1"),
                artifact_url="artifact://unsafe",
                artifact_sha256="a" * 64,
                sdk_version="1",
            )
    await engine.dispose()
