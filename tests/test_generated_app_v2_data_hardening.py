from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from benji_api.db.base import Base
from benji_api.models.channel import Conversation
from benji_api.models.generated_app import GeneratedApp
from benji_api.models.generated_app_v2 import (
    GeneratedAppAccessTicket,
    GeneratedAppDataRecord,
    GeneratedAppDeployment,
    GeneratedAppEvent,
    GeneratedAppRevision,
    GeneratedAppSession,
)
from benji_api.models.user import User
from benji_api.services.generated_apps_v2 import (
    AppActor,
    CodeAppAuthorizationError,
    CodeAppRateLimitError,
    CodeAppValidationError,
    claim_next_build,
    complete_build,
    create_code_app_build,
    create_data_record,
    queue_code_app_revision,
    redeem_access_ticket,
    rollback_owned_code_app,
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
        user = User(phone_number="+14155550880")
        session.add(user)
        await session.flush()
        conversation = Conversation(user_id=user.id)
        session.add(conversation)
        await session.commit()
        return user, conversation


async def _finish(factory, *, worker: str, manifest=MANIFEST):
    async with factory() as session:
        claim = await claim_next_build(session, worker_id=worker)
        assert claim is not None
        return await complete_build(
            session,
            job_id=claim.job_id,
            worker_id=worker,
            expected_attempt=claim.attempt,
            manifest=manifest,
            source_files={"src/App.tsx": "export default function App() { return null }"},
            artifact={},
            artifact_url=f"artifact://{worker}",
            artifact_sha256="a" * 64,
            sdk_version="1.0.0",
        )


@pytest.mark.anyio
async def test_seed_data_is_staged_until_success_and_never_overwrites_user_data() -> None:
    engine, factory = await _database()
    owner, conversation = await _owner(factory)
    async with factory() as session:
        app, _, _ = await create_code_app_build(
            session,
            user_id=owner.id,
            conversation_id=conversation.id,
            title="Launch list",
            description="Original",
            request={
                "blueprint": {
                    "manifest": MANIFEST,
                    "seed_data": {"task": [{"title": "Seed one", "done": False}]},
                }
            },
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(GeneratedAppDataRecord)
                .where(GeneratedAppDataRecord.app_id == app.id)
            )
            == 0
        )
    first = await _finish(factory, worker="initial")
    async with factory() as session:
        seeded = list(
            (
                await session.scalars(
                    select(GeneratedAppDataRecord).where(
                        GeneratedAppDataRecord.app_id == app.id
                    )
                )
            ).all()
        )
        assert [record.data["title"] for record in seeded] == ["Seed one"]
        assert seeded[0].data_bytes > 0
        persisted = await session.get(GeneratedAppRevision, first.id)
        assert persisted is not None and persisted.seed_applied_at is not None
        await create_data_record(
            session,
            app_id=app.id,
            actor=AppActor(role="owner", user_id=owner.id, identity_verified=True),
            entity="task",
            data={"title": "User task", "done": False},
            idempotency_key="user-task-create",
        )
        await queue_code_app_revision(
            session,
            user_id=owner.id,
            app_id=app.id,
            request={
                "blueprint": {
                    "manifest": MANIFEST,
                    "seed_data": {"task": [{"title": "Replacement", "done": False}]},
                },
                "app_metadata": {"title": "Revised", "description": "New"},
            },
        )
    second = await _finish(factory, worker="revision")
    async with factory() as session:
        records = list(
            (
                await session.scalars(
                    select(GeneratedAppDataRecord)
                    .where(GeneratedAppDataRecord.app_id == app.id)
                    .order_by(GeneratedAppDataRecord.created_at)
                )
            ).all()
        )
        assert [record.data["title"] for record in records] == ["Seed one", "User task"]
        revised = await session.get(GeneratedAppRevision, second.id)
        assert revised is not None and revised.seed_applied_at is not None
    await engine.dispose()


@pytest.mark.anyio
async def test_revision_metadata_is_immutable_and_rollback_restores_it() -> None:
    engine, factory = await _database()
    owner, conversation = await _owner(factory)
    async with factory() as session:
        app, _, _ = await create_code_app_build(
            session,
            user_id=owner.id,
            conversation_id=conversation.id,
            title="First title",
            description="First description",
            request={"blueprint": {"manifest": MANIFEST, "seed_data": {}}},
        )
    first = await _finish(factory, worker="first")
    async with factory() as session:
        await queue_code_app_revision(
            session,
            user_id=owner.id,
            app_id=app.id,
            request={
                "blueprint": {"manifest": MANIFEST, "seed_data": {}},
                "app_metadata": {"title": "Second title", "description": "Second description"},
            },
        )
    second = await _finish(factory, worker="second")
    async with factory() as session:
        revised_app = await session.get(GeneratedApp, app.id)
        assert revised_app is not None
        assert (revised_app.title, revised_app.description) == (
            "Second title",
            "Second description",
        )
        assert (first.title, first.description) == ("First title", "First description")
        assert (second.title, second.description) == ("Second title", "Second description")
        await rollback_owned_code_app(
            session,
            user_id=owner.id,
            app_id=app.id,
            expected_active_revision_id=second.id,
        )
    async with factory() as session:
        rolled_back = await session.get(GeneratedApp, app.id)
        deployment = await session.get(GeneratedAppDeployment, app.id)
        assert rolled_back is not None and deployment is not None
        assert deployment.active_revision_id == first.id
        assert (rolled_back.title, rolled_back.description) == (
            "First title",
            "First description",
        )
    await engine.dispose()


@pytest.mark.anyio
async def test_deploy_revalidates_records_written_after_revision_was_queued() -> None:
    engine, factory = await _database()
    owner, conversation = await _owner(factory)
    async with factory() as session:
        app, _, _ = await create_code_app_build(
            session,
            user_id=owner.id,
            conversation_id=conversation.id,
            title="Live schema",
            description="",
            request={"blueprint": {"manifest": MANIFEST, "seed_data": {}}},
        )
    first = await _finish(factory, worker="schema-first")
    incompatible = {
        "schema_version": 1,
        "entities": [
            {
                "name": "task",
                "fields": {
                    "title": {"type": "string", "required": True},
                },
            }
        ],
    }
    async with factory() as session:
        await queue_code_app_revision(
            session,
            user_id=owner.id,
            app_id=app.id,
            request={"blueprint": {"manifest": incompatible, "seed_data": {}}},
        )
        await create_data_record(
            session,
            app_id=app.id,
            actor=AppActor(role="owner", user_id=owner.id, identity_verified=True),
            entity="task",
            data={"title": "Written while building", "done": False},
            idempotency_key="schema-race-record",
        )
    async with factory() as session:
        claim = await claim_next_build(session, worker_id="schema-second")
        assert claim is not None
        with pytest.raises(CodeAppValidationError, match="incompatible"):
            await complete_build(
                session,
                job_id=claim.job_id,
                worker_id="schema-second",
                expected_attempt=claim.attempt,
                manifest=incompatible,
                source_files={"src/App.tsx": "export default function App() { return null }"},
                artifact={},
                artifact_url="artifact://schema-second",
                artifact_sha256="b" * 64,
                sdk_version="1.0.0",
            )
        await session.rollback()
    async with factory() as session:
        deployment = await session.get(GeneratedAppDeployment, app.id)
        assert deployment is not None and deployment.active_revision_id == first.id
    await engine.dispose()


@pytest.mark.anyio
async def test_rollback_rejects_a_previous_schema_incompatible_with_live_data() -> None:
    engine, factory = await _database()
    owner, conversation = await _owner(factory)
    first_manifest = {
        "schema_version": 1,
        "entities": [
            {
                "name": "task",
                "fields": {"title": {"type": "string", "required": True}},
            }
        ],
    }
    async with factory() as session:
        app, _, _ = await create_code_app_build(
            session,
            user_id=owner.id,
            conversation_id=conversation.id,
            title="Rollback schema",
            description="",
            request={"blueprint": {"manifest": first_manifest, "seed_data": {}}},
        )
    first = await _finish(factory, worker="rollback-schema-first", manifest=first_manifest)
    async with factory() as session:
        await queue_code_app_revision(
            session,
            user_id=owner.id,
            app_id=app.id,
            request={"blueprint": {"manifest": MANIFEST, "seed_data": {}}},
        )
    second = await _finish(factory, worker="rollback-schema-second")
    async with factory() as session:
        await create_data_record(
            session,
            app_id=app.id,
            actor=AppActor(role="owner", user_id=owner.id, identity_verified=True),
            entity="task",
            data={"title": "Needs the new field", "done": False},
            idempotency_key="rollback-schema-record",
        )
        with pytest.raises(CodeAppValidationError, match="incompatible"):
            await rollback_owned_code_app(
                session,
                user_id=owner.id,
                app_id=app.id,
                expected_active_revision_id=second.id,
            )
        await session.rollback()
    async with factory() as session:
        deployment = await session.get(GeneratedAppDeployment, app.id)
        assert deployment is not None
        assert deployment.active_revision_id == second.id
        assert deployment.active_revision_id != first.id
    await engine.dispose()


@pytest.mark.anyio
async def test_ticket_redemption_and_app_wide_storage_quotas_are_durable() -> None:
    engine, factory = await _database()
    owner, conversation = await _owner(factory)
    async with factory() as session:
        app, _, ticket = await create_code_app_build(
            session,
            user_id=owner.id,
            conversation_id=conversation.id,
            title="Quota app",
            description="",
            request={"blueprint": {"manifest": MANIFEST, "seed_data": {}}},
        )
    await _finish(factory, worker="quota")
    for _ in range(12):
        async with factory() as session:
            await redeem_access_ticket(session, public_id=app.public_id, token=ticket)
    async with factory() as session:
        with pytest.raises(CodeAppAuthorizationError, match="redemption limit"):
            await redeem_access_ticket(session, public_id=app.public_id, token=ticket)
        persisted_ticket = await session.scalar(
            select(GeneratedAppAccessTicket).where(GeneratedAppAccessTicket.app_id == app.id)
        )
        assert persisted_ticket is not None and persisted_ticket.redemption_count == 12

        # A new bearer session cannot bypass an app-wide durable storage quota.
        from benji_api.services import generated_apps_v2 as service

        old_limit = service._MAX_DATA_BYTES_PER_APP
        service._MAX_DATA_BYTES_PER_APP = 1
        try:
            with pytest.raises(CodeAppValidationError, match="storage limit"):
                await create_data_record(
                    session,
                    app_id=app.id,
                    actor=AppActor(role="member", session_id=None, user_id=owner.id),
                    entity="task",
                    data={"title": "No room", "done": False},
                    idempotency_key="quota-record-create",
                )
        finally:
            service._MAX_DATA_BYTES_PER_APP = old_limit
    await engine.dispose()


@pytest.mark.anyio
async def test_app_wide_mutation_quota_cannot_be_bypassed_by_session_cycling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, factory = await _database()
    owner, conversation = await _owner(factory)
    async with factory() as session:
        app, _, _ = await create_code_app_build(
            session,
            user_id=owner.id,
            conversation_id=conversation.id,
            title="Shared quota app",
            description="",
            request={"blueprint": {"manifest": MANIFEST, "seed_data": {}}},
        )
    await _finish(factory, worker="shared-quota")
    from benji_api.services import generated_apps_v2 as service

    monkeypatch.setattr(service, "_MAX_MUTATIONS_PER_APP_MINUTE", 2)
    async with factory() as session:
        sessions = [
            GeneratedAppSession(
                app_id=app.id,
                role="member",
                token_hash=str(index) * 64,
                expires_at=datetime.now(UTC) + timedelta(days=1),
            )
            for index in (1, 2)
        ]
        session.add_all(sessions)
        await session.flush()
        session.add_all(
            [
                GeneratedAppEvent(
                    app_id=app.id,
                    event_type="app.data.created",
                    actor_session_id=app_session.id,
                    idempotency_key=f"cycled-session-{index}",
                    payload={},
                )
                for index, app_session in enumerate(sessions)
            ]
        )
        await session.commit()
        with pytest.raises(CodeAppRateLimitError, match="too much"):
            await create_data_record(
                session,
                app_id=app.id,
                actor=AppActor(role="owner", user_id=owner.id),
                entity="task",
                data={"title": "Third mutation", "done": False},
                idempotency_key="global-rate-limit",
            )
    await engine.dispose()
