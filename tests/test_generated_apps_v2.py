from datetime import UTC, datetime, timedelta

import pytest
from generated_app_artifacts import compiled_artifact
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from benji_api.api.dependencies import get_optional_authenticated_user
from benji_api.db.base import Base
from benji_api.db.session import get_session
from benji_api.main import app as api
from benji_api.models.channel import Conversation, ConversationKind, ConversationMember
from benji_api.models.generated_app_v2 import (
    GeneratedAppAccessTicket,
    GeneratedAppBuildJob,
    GeneratedAppBuildStatus,
    GeneratedAppEvent,
    GeneratedAppRevision,
    GeneratedAppSession,
)
from benji_api.models.user import User
from benji_api.models.user_event import UserEvent
from benji_api.services.generated_apps import archive_generated_app
from benji_api.services.generated_apps_v2 import (
    AppActor,
    CodeAppAuthorizationError,
    CodeAppConflictError,
    CodeAppNotFoundError,
    CodeAppValidationError,
    _validate_promotable_compiled_artifact,
    authorize_session,
    claim_next_build,
    complete_build,
    create_code_app_build,
    create_data_record,
    delete_data_record,
    fail_build,
    redeem_access_ticket,
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


def test_only_real_browser_verified_bundles_are_promotable() -> None:
    artifact = compiled_artifact(sdk_version="1")
    _validate_promotable_compiled_artifact(artifact, sdk_version="1")

    without_bundle = dict(artifact)
    without_bundle.pop("browser_bundle")
    with pytest.raises(CodeAppValidationError, match="compiled browser bundle"):
        _validate_promotable_compiled_artifact(without_bundle, sdk_version="1")

    without_real_browser = compiled_artifact(sdk_version="1")
    without_real_browser["test_results"]["browser_smoke"].pop("real_browser")
    with pytest.raises(CodeAppValidationError, match="real-browser acceptance"):
        _validate_promotable_compiled_artifact(without_real_browser, sdk_version="1")


@pytest.mark.anyio
async def test_code_app_build_session_runtime_and_optimistic_data() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with factory() as session:
        user = User(phone_number="+14155552671")
        session.add(user)
        await session.flush()
        conversation = Conversation(user_id=user.id)
        session.add(conversation)
        await session.commit()
        code_app, job, ticket = await create_code_app_build(
            session,
            user_id=user.id,
            conversation_id=conversation.id,
            title="Birthday plan",
            description="Plan a party",
            request={
                "prompt": "make a birthday planner",
                "blueprint": {"seed_data": {"occasion": "birthday"}},
            },
            delivery_provider="linq",
        )
    async with factory() as session:
        claim = await claim_next_build(session, worker_id="builder-1", lease_seconds=60)
        assert claim is not None
        assert claim.job_id == job.id
        revision = await complete_build(
            session,
            job_id=claim.job_id,
            worker_id="builder-1",
            expected_attempt=claim.attempt,
            manifest=MANIFEST,
            source_files={"src/App.tsx": "export default function App() {}"},
            artifact=compiled_artifact(sdk_version="1.0.0"),
            artifact_url="https://assets.example/revision.json",
            artifact_sha256="a" * 64,
            sdk_version="1.0.0",
            test_results={"passed": True},
            app_url=f"https://app.example/apps/{code_app.public_id}",
        )
        assert revision.revision_number == 1
        event = await session.scalar(
            select(UserEvent).where(UserEvent.event_type == "app.build.completed")
        )
        assert event is not None
        assert event.conversation_id == conversation.id
        assert event.delivery_provider == "linq"
        assert event.payload["app_id"] == str(code_app.id)
        assert "fallback_mode" not in event.payload

    async def override_session():
        async with factory() as session:
            yield session

    api.dependency_overrides[get_session] = override_session
    api.dependency_overrides[get_optional_authenticated_user] = lambda: None
    try:
        async with AsyncClient(transport=ASGITransport(app=api), base_url="http://test") as client:
            opened = await client.get(f"/api/v1/apps/v2/{code_app.public_id}")
            assert opened.status_code == 200
            assert opened.json()["runtime_kind"] == "code"
            assert opened.json()["active_revision"] is None
            unauthorized = await client.post(
                f"/api/v1/apps/v2/{code_app.id}/actions",
                json={"operation": "records.list", "args": {"entity": "task"}},
            )
            assert unauthorized.status_code == 403
            redeemed = await client.post(
                f"/api/v1/apps/v2/{code_app.public_id}/sessions/redeem",
                json={"ticket": ticket},
            )
            assert redeemed.status_code == 200
            token = redeemed.json()["session_token"]
            repeated_redemption = await client.post(
                f"/api/v1/apps/v2/{code_app.public_id}/sessions/redeem",
                json={"ticket": ticket},
            )
            assert repeated_redemption.status_code == 200
            headers = {"X-Dot-App-Session": token}
            authorized = await client.get(
                f"/api/v1/apps/v2/{code_app.public_id}",
                headers=headers,
            )
            runtime_artifact = authorized.json()["active_revision"]["artifact"]
            assert runtime_artifact["browser_bundle"]["format"] == "iife"
            assert set(runtime_artifact) == {"browser_bundle"}
            assert authorized.json()["active_revision"]["seed_data"] == {
                "occasion": "birthday"
            }
            app_data = await client.post(
                f"/api/v1/apps/v2/{code_app.id}/actions",
                headers=headers,
                json={"operation": "app.data.get", "args": {}},
            )
            assert app_data.status_code == 200
            assert app_data.json()["data"] == {"role": "owner"}
            created = await client.post(
                f"/api/v1/apps/v2/{code_app.id}/actions",
                headers=headers,
                json={
                    "operation": "records.create",
                    "idempotency_key": "create-task-0001",
                    "args": {"entity": "task", "data": {"title": "Cake", "done": False}},
                },
            )
            assert created.status_code == 200
            record = created.json()["data"]
            repeated = await client.post(
                f"/api/v1/apps/v2/{code_app.id}/actions",
                headers=headers,
                json={
                    "operation": "records.create",
                    "idempotency_key": "create-task-0001",
                    "args": {"entity": "task", "data": {"title": "Cake", "done": False}},
                },
            )
            assert repeated.json()["data"]["id"] == record["id"]
            stale = await client.post(
                f"/api/v1/apps/v2/{code_app.id}/actions",
                headers=headers,
                json={
                    "operation": "records.update",
                    "idempotency_key": "update-task-0001",
                    "args": {
                        "record_id": record["id"],
                        "expected_version": 2,
                        "data": {"title": "Cake", "done": True},
                    },
                },
            )
            assert stale.status_code == 409
            updated = await client.post(
                f"/api/v1/apps/v2/{code_app.id}/actions",
                headers=headers,
                json={
                    "operation": "records.update",
                    "idempotency_key": "update-task-0002",
                    "args": {
                        "record_id": record["id"],
                        "expected_version": 1,
                        "data": {"title": "Cake", "done": True},
                    },
                },
            )
            assert updated.status_code == 200
            assert updated.json()["data"]["version"] == 2
            listed = await client.post(
                f"/api/v1/apps/v2/{code_app.id}/actions",
                headers=headers,
                json={"operation": "records.list", "args": {"entity": "task"}},
            )
            assert listed.json()["meta"]["total"] == 1
            assert listed.json()["data"][0]["data"]["done"] is True
    finally:
        api.dependency_overrides.clear()
        async with factory() as session:
            events = list((await session.scalars(select(GeneratedAppEvent))).all())
            assert [event.event_type for event in events] == [
                "app.data.created",
                "app.data.updated",
            ]
        await engine.dispose()


@pytest.mark.anyio
async def test_only_claim_owner_can_fail_build_and_failure_is_event_driven() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with factory() as session:
        user = User(phone_number="+14155552671")
        session.add(user)
        await session.flush()
        conversation = Conversation(user_id=user.id)
        session.add(conversation)
        await session.commit()
        _, job, _ = await create_code_app_build(
            session,
            user_id=user.id,
            conversation_id=conversation.id,
            title="Tracker",
            description="",
            request={"prompt": "make a tracker"},
        )
    async with factory() as session:
        claim = await claim_next_build(session, worker_id="builder-1")
        assert claim is not None
        with pytest.raises(CodeAppConflictError):
            await fail_build(
                session,
                job_id=job.id,
                worker_id="other-builder",
                expected_attempt=claim.attempt,
                error="not mine",
            )
        failed = await fail_build(
            session,
            job_id=job.id,
            worker_id="builder-1",
            expected_attempt=claim.attempt,
            error="browser test failed",
        )
        assert failed.status == GeneratedAppBuildStatus.FAILED.value
        event = await session.scalar(select(UserEvent))
        assert event is not None
        assert event.event_type == "app.build.failed"
    await engine.dispose()


@pytest.mark.anyio
async def test_reclaimed_build_fences_stale_completion_and_failure() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with factory() as session:
        user = User(phone_number="+14155552672")
        session.add(user)
        await session.flush()
        conversation = Conversation(user_id=user.id)
        session.add(conversation)
        await session.flush()
        _, job, _ = await create_code_app_build(
            session,
            user_id=user.id,
            conversation_id=conversation.id,
            title="Fenced build",
            description="",
            request={"prompt": "make a tracker"},
        )

    async with factory() as session:
        stale_claim = await claim_next_build(session, worker_id="builder-old")
        assert stale_claim is not None and stale_claim.attempt == 1
        claimed_job = await session.get(GeneratedAppBuildJob, job.id)
        assert claimed_job is not None
        claimed_job.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()

    async with factory() as session:
        current_claim = await claim_next_build(session, worker_id="builder-new")
        assert current_claim is not None and current_claim.attempt == 2

    async with factory() as session:
        with pytest.raises(CodeAppConflictError, match="stale"):
            await complete_build(
                session,
                job_id=stale_claim.job_id,
                worker_id="builder-old",
                expected_attempt=stale_claim.attempt,
                manifest=MANIFEST,
                source_files={"src/App.tsx": "export default function App() {}"},
                artifact=compiled_artifact(sdk_version="1"),
                artifact_url="artifact://stale",
                artifact_sha256="a" * 64,
                sdk_version="1",
            )
        with pytest.raises(CodeAppConflictError, match="stale"):
            await fail_build(
                session,
                job_id=stale_claim.job_id,
                worker_id="builder-old",
                expected_attempt=stale_claim.attempt,
                error="stale worker failed late",
            )

    async with factory() as session:
        await complete_build(
            session,
            job_id=current_claim.job_id,
            worker_id="builder-new",
            expected_attempt=current_claim.attempt,
            manifest=MANIFEST,
            source_files={"src/App.tsx": "export default function App() {}"},
            artifact=compiled_artifact(sdk_version="1"),
            artifact_url="artifact://current",
            artifact_sha256="b" * 64,
            sdk_version="1",
        )

    async with factory() as session:
        settled_job = await session.get(GeneratedAppBuildJob, job.id)
        assert settled_job is not None
        assert settled_job.status == GeneratedAppBuildStatus.SUCCEEDED.value
        assert settled_job.attempts == 2
        assert (
            await session.scalar(select(func.count()).select_from(GeneratedAppRevision))
            == 1
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(UserEvent)
                .where(UserEvent.event_type == "app.build.completed")
            )
            == 1
        )
    await engine.dispose()


@pytest.mark.anyio
async def test_archiving_code_app_revokes_bearers_and_blocks_stale_mutations() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with factory() as session:
        owner = User(phone_number="+14155552673")
        session.add(owner)
        await session.flush()
        conversation = Conversation(user_id=owner.id)
        session.add(conversation)
        await session.flush()
        code_app, _, ticket = await create_code_app_build(
            session,
            user_id=owner.id,
            conversation_id=conversation.id,
            title="Archive me",
            description="",
            request={"prompt": "make a tracker"},
        )

    async with factory() as session:
        claim = await claim_next_build(session, worker_id="archive-builder")
        assert claim is not None
        await complete_build(
            session,
            job_id=claim.job_id,
            worker_id="archive-builder",
            expected_attempt=claim.attempt,
            manifest=MANIFEST,
            source_files={"src/App.tsx": "export default function App() {}"},
            artifact=compiled_artifact(sdk_version="1"),
            artifact_url="artifact://archive-test",
            artifact_sha256="c" * 64,
            sdk_version="1",
        )
        token, app_session = await redeem_access_ticket(
            session,
            public_id=code_app.public_id,
            token=ticket,
        )
        record = await create_data_record(
            session,
            app_id=code_app.id,
            actor=AppActor(
                role=app_session.role,
                user_id=app_session.user_id,
                session_id=app_session.id,
            ),
            entity="task",
            data={"title": "Keep me", "done": False},
            idempotency_key="archive-create-record",
        )

    async with factory() as session:
        archived = await archive_generated_app(
            session,
            user_id=owner.id,
            app_id=code_app.id,
        )
        assert archived is not None
        await session.commit()
        with pytest.raises(CodeAppAuthorizationError):
            await authorize_session(session, app_id=code_app.id, token=token)
        stored_session = await session.get(GeneratedAppSession, app_session.id)
        assert stored_session is not None and stored_session.revoked_at is not None
        stored_ticket = await session.scalar(
            select(GeneratedAppAccessTicket).where(
                GeneratedAppAccessTicket.app_id == code_app.id,
                GeneratedAppAccessTicket.token_hash.is_not(None),
            )
        )
        assert stored_ticket is not None
        ticket_expiry = stored_ticket.expires_at
        if ticket_expiry.tzinfo is None:
            ticket_expiry = ticket_expiry.replace(tzinfo=UTC)
        assert ticket_expiry <= datetime.now(UTC)
        with pytest.raises(CodeAppNotFoundError):
            await delete_data_record(
                session,
                app_id=code_app.id,
                record_id=record.id,
                actor=AppActor(
                    role="owner",
                    user_id=owner.id,
                    identity_verified=True,
                ),
                expected_version=record.version,
                idempotency_key="archive-delete-record",
            )
    await engine.dispose()


@pytest.mark.anyio
async def test_revoked_session_snapshot_cannot_mutate_app_data() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with factory() as session:
        owner = User(phone_number="+14155552679")
        session.add(owner)
        await session.flush()
        conversation = Conversation(user_id=owner.id)
        session.add(conversation)
        await session.flush()
        code_app, _, _ = await create_code_app_build(
            session,
            user_id=owner.id,
            conversation_id=conversation.id,
            title="Revoked bearer",
            description="",
            request={"prompt": "make a list"},
        )
        app_session = GeneratedAppSession(
            app_id=code_app.id,
            role="member",
            token_hash="f" * 64,
            expires_at=datetime.now(UTC) + timedelta(days=1),
        )
        session.add(app_session)
        await session.commit()
        stale_actor = AppActor(
            role="member",
            session_id=app_session.id,
        )

    async with factory() as session:
        persisted = await session.get(GeneratedAppSession, app_session.id)
        assert persisted is not None
        persisted.revoked_at = datetime.now(UTC)
        await session.commit()

    async with factory() as session:
        with pytest.raises(CodeAppAuthorizationError, match="invalid or expired"):
            await create_data_record(
                session,
                app_id=code_app.id,
                actor=stale_actor,
                entity="task",
                data={"title": "Should not exist", "done": False},
                idempotency_key="revoked-session-mutation",
            )
        assert await session.scalar(select(func.count()).select_from(GeneratedAppEvent)) == 0
    await engine.dispose()


@pytest.mark.anyio
async def test_expired_crash_claims_terminalize_once_then_claim_next_job() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with factory() as session:
        user = User(phone_number="+14155552673")
        session.add(user)
        await session.flush()
        conversation = Conversation(user_id=user.id)
        session.add(conversation)
        await session.flush()
        _, abandoned_job, _ = await create_code_app_build(
            session,
            user_id=user.id,
            conversation_id=conversation.id,
            title="Abandoned build",
            description="",
            request={"prompt": "make a tracker"},
        )

    for expected_attempt in (1, 2, 3):
        async with factory() as session:
            crash_claim = await claim_next_build(
                session,
                worker_id=f"crashed-builder-{expected_attempt}",
            )
            assert crash_claim is not None
            assert crash_claim.job_id == abandoned_job.id
            assert crash_claim.attempt == expected_attempt
            claimed_job = await session.get(GeneratedAppBuildJob, abandoned_job.id)
            assert claimed_job is not None
            claimed_job.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()

    async with factory() as session:
        _, next_job, _ = await create_code_app_build(
            session,
            user_id=user.id,
            conversation_id=conversation.id,
            title="Next build",
            description="",
            request={"prompt": "make another tracker"},
        )

    async with factory() as session:
        next_claim = await claim_next_build(session, worker_id="healthy-builder")
        assert next_claim is not None
        assert next_claim.job_id == next_job.id
        assert next_claim.attempt == 1

    async with factory() as session:
        failed_job = await session.get(GeneratedAppBuildJob, abandoned_job.id)
        assert failed_job is not None
        assert failed_job.status == GeneratedAppBuildStatus.FAILED.value
        assert failed_job.attempts == 3
        assert failed_job.result["failure_code"] == "build_worker_lease_exhausted"
        events = list(
            (
                await session.scalars(
                    select(UserEvent).where(
                        UserEvent.idempotency_key == f"app-build-failed:{abandoned_job.id}"
                    )
                )
            ).all()
        )
        assert len(events) == 1
        assert events[0].event_type == "app.build.failed"
        assert events[0].payload["failure_code"] == "build_worker_lease_exhausted"
        assert await claim_next_build(session, worker_id="idle-builder") is None
        assert (
            await session.scalar(
                select(func.count())
                .select_from(UserEvent)
                .where(UserEvent.idempotency_key == f"app-build-failed:{abandoned_job.id}")
            )
            == 1
        )
    await engine.dispose()


@pytest.mark.anyio
async def test_group_app_creation_requests_conversation_owner_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with factory() as session:
        owner = User(phone_number="+14155552674")
        session.add(owner)
        await session.flush()
        group = Conversation(user_id=owner.id, kind=ConversationKind.GROUP.value)
        session.add(group)
        await session.flush()
        session.add(
            ConversationMember(
                conversation_id=group.id,
                user_id=owner.id,
                external_handle=owner.phone_number,
                role="owner",
            )
        )
        await session.commit()

    observed_owner_lock = False
    session_type = type(session)
    original_scalar = session_type.scalar

    async def scalar_with_observation(self, statement, *args, **kwargs):
        nonlocal observed_owner_lock
        if "FROM conversations" in str(statement):
            observed_owner_lock = observed_owner_lock or (
                getattr(statement, "_for_update_arg", None) is not None
            )
        return await original_scalar(self, statement, *args, **kwargs)

    monkeypatch.setattr(session_type, "scalar", scalar_with_observation)
    async with factory() as create_session:
        monkeypatch.setattr(create_session.bind.dialect, "name", "postgresql")
        await create_code_app_build(
            create_session,
            user_id=owner.id,
            conversation_id=group.id,
            title="Locked group app",
            description="",
            request={"blueprint": {"title": "Locked group app"}},
        )

    assert observed_owner_lock is True
    await engine.dispose()
