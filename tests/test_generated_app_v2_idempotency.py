import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from benji_api.api.dependencies import get_optional_authenticated_user
from benji_api.db.base import Base
from benji_api.db.session import get_session
from benji_api.main import app as api
from benji_api.models.channel import Conversation
from benji_api.models.generated_app import GeneratedApp
from benji_api.models.generated_app_v2 import (
    GeneratedAppBuildJob,
    GeneratedAppBuildStatus,
    GeneratedAppEvent,
)
from benji_api.models.user import User
from benji_api.services import generated_apps_v2 as service
from benji_api.services.generated_apps_v2 import (
    CodeAppRateLimitError,
    CodeAppValidationError,
    claim_next_build,
    complete_build,
    create_code_app_build,
    queue_code_app_revision,
)

MANIFEST = {
    "schema_version": 1,
    "entities": [
        {
            "name": "task",
            "fields": {
                "title": {"type": "string", "required": True},
                "done": {"type": "boolean", "required": True},
            },
        }
    ],
}


async def _database():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, factory


async def _owner(factory):
    async with factory() as session:
        owner = User(phone_number="+14155550891")
        session.add(owner)
        await session.flush()
        conversation = Conversation(user_id=owner.id)
        session.add(conversation)
        await session.commit()
        return owner, conversation


async def _deployed_app(factory):
    owner, conversation = await _owner(factory)
    async with factory() as session:
        app, _, _ = await create_code_app_build(
            session,
            user_id=owner.id,
            conversation_id=conversation.id,
            title="Exact replay",
            description="",
            request={"blueprint": {"manifest": MANIFEST, "seed_data": {}}},
        )
    async with factory() as session:
        claim = await claim_next_build(session, worker_id="idempotency-test")
        assert claim is not None
        await complete_build(
            session,
            job_id=claim.job_id,
            worker_id="idempotency-test",
            expected_attempt=claim.attempt,
            manifest=MANIFEST,
            source_files={"src/App.tsx": "export default function App() { return null }"},
            artifact={},
            artifact_url="artifact://idempotency-test",
            artifact_sha256="a" * 64,
            sdk_version="1",
        )
    return owner, app


def _mutation(operation: str, key: str, args: dict) -> dict:
    return {"operation": operation, "idempotency_key": key, "args": args}


@pytest.mark.anyio
async def test_record_mutations_replay_exact_snapshots_and_reject_key_reuse() -> None:
    engine, factory = await _database()
    owner, app = await _deployed_app(factory)

    async def override_session():
        async with factory() as session:
            yield session

    api.dependency_overrides[get_session] = override_session
    api.dependency_overrides[get_optional_authenticated_user] = lambda: owner
    try:
        async with AsyncClient(transport=ASGITransport(app=api), base_url="http://test") as client:
            create_request = _mutation(
                "records.create",
                "exact-create-key",
                {"entity": "task", "data": {"title": "First", "done": False}},
            )
            created = await client.post(
                f"/api/v1/apps/v2/{app.id}/actions", json=create_request
            )
            replayed_create = await client.post(
                f"/api/v1/apps/v2/{app.id}/actions", json=create_request
            )
            assert created.status_code == replayed_create.status_code == 200
            assert replayed_create.json() == created.json()
            first = created.json()["data"]

            changed_payload = _mutation(
                "records.create",
                "exact-create-key",
                {"entity": "task", "data": {"title": "Different", "done": False}},
            )
            assert (
                await client.post(
                    f"/api/v1/apps/v2/{app.id}/actions", json=changed_payload
                )
            ).status_code == 409
            cross_operation = _mutation(
                "records.delete",
                "exact-create-key",
                {"record_id": first["id"], "expected_version": 1},
            )
            assert (
                await client.post(
                    f"/api/v1/apps/v2/{app.id}/actions", json=cross_operation
                )
            ).status_code == 409

            second_created = await client.post(
                f"/api/v1/apps/v2/{app.id}/actions",
                json=_mutation(
                    "records.create",
                    "second-create-key",
                    {"entity": "task", "data": {"title": "Second", "done": False}},
                ),
            )
            second = second_created.json()["data"]

            first_update_request = _mutation(
                "records.update",
                "first-update-key",
                {
                    "record_id": first["id"],
                    "expected_version": 1,
                    "data": {"title": "First updated", "done": True},
                },
            )
            first_update = await client.post(
                f"/api/v1/apps/v2/{app.id}/actions", json=first_update_request
            )
            assert first_update.status_code == 200
            later_update = await client.post(
                f"/api/v1/apps/v2/{app.id}/actions",
                json=_mutation(
                    "records.update",
                    "later-update-key",
                    {
                        "record_id": first["id"],
                        "expected_version": 2,
                        "data": {"title": "Changed again", "done": True},
                    },
                ),
            )
            assert later_update.json()["data"]["version"] == 3
            replayed_update = await client.post(
                f"/api/v1/apps/v2/{app.id}/actions", json=first_update_request
            )
            assert replayed_update.status_code == 200
            assert replayed_update.json() == first_update.json()
            assert replayed_update.json()["data"]["version"] == 2

            wrong_target_update = _mutation(
                "records.update",
                "first-update-key",
                {
                    "record_id": second["id"],
                    "expected_version": 1,
                    "data": {"title": "First updated", "done": True},
                },
            )
            assert (
                await client.post(
                    f"/api/v1/apps/v2/{app.id}/actions", json=wrong_target_update
                )
            ).status_code == 409

            delete_request = _mutation(
                "records.delete",
                "exact-delete-key",
                {"record_id": first["id"], "expected_version": 3},
            )
            deleted = await client.post(
                f"/api/v1/apps/v2/{app.id}/actions", json=delete_request
            )
            replayed_delete = await client.post(
                f"/api/v1/apps/v2/{app.id}/actions", json=delete_request
            )
            assert deleted.status_code == replayed_delete.status_code == 200
            assert replayed_delete.json() == deleted.json()
            replayed_create_after_delete = await client.post(
                f"/api/v1/apps/v2/{app.id}/actions", json=create_request
            )
            assert replayed_create_after_delete.status_code == 200
            assert replayed_create_after_delete.json() == created.json()
            wrong_delete_target = _mutation(
                "records.delete",
                "exact-delete-key",
                {"record_id": second["id"], "expected_version": 1},
            )
            assert (
                await client.post(
                    f"/api/v1/apps/v2/{app.id}/actions", json=wrong_delete_target
                )
            ).status_code == 409
    finally:
        api.dependency_overrides.clear()

    async with factory() as session:
        events = list(
            (
                await session.scalars(
                    select(GeneratedAppEvent).order_by(GeneratedAppEvent.created_at)
                )
            ).all()
        )
        assert len(events) == 5
        assert all(event.operation and len(event.request_hash or "") == 64 for event in events)
        deleted_event = next(event for event in events if event.operation == "records.delete")
        assert deleted_event.payload["tombstone"] is True
        assert deleted_event.response["deleted"] is True
    await engine.dispose()


@pytest.mark.anyio
async def test_user_app_and_live_build_quotas_apply_before_enqueue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, factory = await _database()
    owner, conversation = await _owner(factory)
    monkeypatch.setattr(service, "_MAX_ACTIVE_CODE_APPS_PER_USER", 2)
    monkeypatch.setattr(service, "_MAX_LIVE_BUILDS_PER_USER", 10)
    async with factory() as session:
        for index in range(2):
            await create_code_app_build(
                session,
                user_id=owner.id,
                conversation_id=conversation.id,
                title=f"App {index}",
                description="",
                request={"blueprint": {"manifest": MANIFEST, "seed_data": {}}},
            )
        with pytest.raises(CodeAppValidationError, match="active custom-app limit"):
            await create_code_app_build(
                session,
                user_id=owner.id,
                conversation_id=conversation.id,
                title="Too many",
                description="",
                request={"blueprint": {"manifest": MANIFEST, "seed_data": {}}},
            )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(GeneratedApp)
                .where(GeneratedApp.user_id == owner.id)
            )
            == 2
        )

        monkeypatch.setattr(service, "_MAX_ACTIVE_CODE_APPS_PER_USER", 20)
        monkeypatch.setattr(service, "_MAX_LIVE_BUILDS_PER_USER", 2)
        with pytest.raises(CodeAppRateLimitError, match="already have several"):
            await create_code_app_build(
                session,
                user_id=owner.id,
                conversation_id=conversation.id,
                title="Build queue full",
                description="",
                request={"blueprint": {"manifest": MANIFEST, "seed_data": {}}},
            )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(GeneratedApp)
                .where(GeneratedApp.user_id == owner.id)
            )
            == 2
        )
    await engine.dispose()


@pytest.mark.anyio
async def test_live_build_quota_also_blocks_paid_revision_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, factory = await _database()
    owner, deployed_app = await _deployed_app(factory)
    async with factory() as session:
        conversation = await session.scalar(
            select(Conversation).where(Conversation.user_id == owner.id)
        )
        assert conversation is not None
        monkeypatch.setattr(service, "_MAX_LIVE_BUILDS_PER_USER", 2)
        for index in range(2):
            await create_code_app_build(
                session,
                user_id=owner.id,
                conversation_id=conversation.id,
                title=f"Queued {index}",
                description="",
                request={"blueprint": {"manifest": MANIFEST, "seed_data": {}}},
            )
        with pytest.raises(CodeAppRateLimitError, match="already have several"):
            await queue_code_app_revision(
                session,
                user_id=owner.id,
                app_id=deployed_app.id,
                request={"blueprint": {"manifest": MANIFEST, "seed_data": {}}},
            )
        live_builds = await session.scalar(
            select(func.count())
            .select_from(GeneratedAppBuildJob)
            .where(
                GeneratedAppBuildJob.status.in_(
                    {
                        GeneratedAppBuildStatus.QUEUED.value,
                        GeneratedAppBuildStatus.CLAIMED.value,
                    }
                )
            )
        )
        assert live_builds == 2
        assert (
            await session.scalar(
                select(func.count())
                .select_from(GeneratedAppBuildJob)
                .where(
                    GeneratedAppBuildJob.app_id == deployed_app.id,
                    GeneratedAppBuildJob.status.in_(
                        {
                            GeneratedAppBuildStatus.QUEUED.value,
                            GeneratedAppBuildStatus.CLAIMED.value,
                        }
                    ),
                )
            )
            == 0
        )
    await engine.dispose()
