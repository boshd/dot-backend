from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from benji_api.agents.tools import (
    CreateCustomAppLinkTool,
    CreateCustomAppRecordTool,
    CreateGeneratedAppTool,
    DeleteCustomAppRecordTool,
    InspectCustomAppTool,
    ListCustomAppRecordsTool,
    ReviseCustomAppTool,
    RollbackCustomAppTool,
    UpdateCustomAppRecordTool,
)
from benji_api.agents.types import ToolContext
from benji_api.config import Settings
from benji_api.db.base import Base
from benji_api.models.channel import Conversation, ConversationKind, ConversationMember
from benji_api.models.generated_app import GeneratedApp, GeneratedAppAccessMode
from benji_api.models.generated_app_v2 import (
    GeneratedAppAccessTicket,
    GeneratedAppBuildJob,
    GeneratedAppBuildStatus,
    GeneratedAppDataRecord,
    GeneratedAppDeployment,
    GeneratedAppEvent,
    GeneratedAppMembership,
    GeneratedAppRevision,
    GeneratedAppRole,
)
from benji_api.models.user import User
from benji_api.models.user_event import UserEvent
from benji_api.services import generated_apps_v2
from benji_api.services.generated_apps_v2 import (
    AppActor,
    CodeAppAuthorizationError,
    CodeAppConflictError,
    CodeAppStaleBuildError,
    CodeAppValidationError,
    authorize_user,
    claim_next_build,
    complete_build,
    create_code_app_build,
    create_data_record,
    delete_data_record,
    fail_build,
    queue_code_app_revision,
    redeem_access_ticket,
    update_data_record,
)

MANIFEST = {
    "schema_version": 1,
    "entities": [
        {
            "name": "task",
            "description": "Things to finish",
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


async def _deployed_app(factory):
    async with factory() as session:
        owner = User(phone_number="+14155550101")
        session.add(owner)
        await session.flush()
        direct = Conversation(user_id=owner.id)
        session.add(direct)
        await session.commit()
        code_app, _, _ = await create_code_app_build(
            session,
            user_id=owner.id,
            conversation_id=direct.id,
            title="Launch list",
            description="Keep launch tasks moving",
            request={
                "blueprint": {
                    "title": "Launch list",
                    "description": "Keep launch tasks moving",
                    "purpose": "Finish the launch",
                    "layout": "launch_board",
                    "accent": "#e7654b",
                    "product_brief": "A focused launch checklist.",
                    "visual_direction": "Editorial and energetic.",
                    "manifest": MANIFEST,
                    "seed_data": {},
                }
            },
        )
    async with factory() as session:
        claim = await claim_next_build(session, worker_id="builder")
        assert claim is not None
        await complete_build(
            session,
            job_id=claim.job_id,
            worker_id="builder",
            expected_attempt=claim.attempt,
            manifest=MANIFEST,
            source_files={"src/App.tsx": "export default function App() { return null }"},
            artifact={"render_document": {"schema_version": 1, "root": {"type": "page"}}},
            artifact_url="artifact://launch-list",
            artifact_sha256="a" * 64,
            sdk_version="1.0.0",
        )
    return owner, direct, code_app


@pytest.mark.anyio
async def test_private_agent_tools_manage_code_app_records_and_queue_revision() -> None:
    engine, factory = await _database()
    owner, direct, code_app = await _deployed_app(factory)
    settings = Settings(generated_app_public_url="https://app.textdot.test")
    context = ToolContext(user_id=owner.id, conversation_id=direct.id)

    inspected = await InspectCustomAppTool(settings, session_factory=factory).execute(
        context=context,
        arguments={"app_id": str(code_app.id)},
    )
    assert inspected["active_revision"]["manifest"] == MANIFEST
    assert inspected["app_url"] == f"https://app.textdot.test/a/{code_app.public_id}"

    fresh_link = await CreateCustomAppLinkTool(
        settings,
        session_factory=factory,
    ).execute(
        context=context,
        arguments={"app_id": str(code_app.id)},
    )
    assert fresh_link["app_url"].startswith(
        f"https://app.textdot.test/a/{code_app.public_id}#handoff="
    )
    assert fresh_link["private"] is True
    fresh_ticket = fresh_link["app_url"].split("#handoff=", maxsplit=1)[1]
    async with factory() as session:
        _, app_session = await redeem_access_ticket(
            session,
            public_id=code_app.public_id,
            token=fresh_ticket,
        )
        assert app_session.user_id == owner.id
        assert app_session.role == GeneratedAppRole.OWNER.value
    async with factory() as session:
        tickets = list(
            (
                await session.scalars(
                    select(GeneratedAppAccessTicket).where(
                        GeneratedAppAccessTicket.app_id == code_app.id
                    )
                )
            ).all()
        )
        assert len(tickets) == 2
        assert all(ticket.principal_user_id == owner.id for ticket in tickets)
        assert all(ticket.role == GeneratedAppRole.OWNER.value for ticket in tickets)
        assert any(ticket.redemption_count == 1 for ticket in tickets)

    created = await CreateCustomAppRecordTool(session_factory=factory).execute(
        context=context,
        arguments={
            "app_id": str(code_app.id),
            "entity": "task",
            "data_json": '{"title":"Ship it","done":false}',
        },
    )
    assert created["record"]["version"] == 1
    record_id = created["record"]["record_id"]
    listed = await ListCustomAppRecordsTool(session_factory=factory).execute(
        context=context,
        arguments={
            "app_id": str(code_app.id),
            "entity": "task",
            "limit": 25,
            "offset": 0,
        },
    )
    assert listed["total"] == 1
    assert listed["records"][0]["data"]["title"] == "Ship it"

    updated = await UpdateCustomAppRecordTool(session_factory=factory).execute(
        context=context,
        arguments={
            "app_id": str(code_app.id),
            "record_id": record_id,
            "expected_version": 1,
            "data_json": '{"title":"Ship it","done":true}',
        },
    )
    assert updated["record"]["version"] == 2
    assert updated["record"]["data"]["done"] is True

    with pytest.raises(CodeAppValidationError, match="incompatible"):
        await ReviseCustomAppTool(session_factory=factory).execute(
            context=context,
            arguments={
                "app_id": str(code_app.id),
                "change_request": "Remove the task data model.",
                "title": None,
                "description": None,
                "visual_direction": None,
                "manifest_json": '{"schema_version":1,"entities":[]}',
                "seed_data_json": None,
            },
        )

    revision_result = await ReviseCustomAppTool(session_factory=factory).execute(
        context=context,
        arguments={
            "app_id": str(code_app.id),
            "change_request": (
                "Make completed tasks feel celebratory and add a compact progress view."
            ),
            "title": None,
            "description": None,
            "visual_direction": "Playful editorial, with a satisfying completed state.",
            "manifest_json": None,
            "seed_data_json": None,
        },
    )
    assert revision_result["status"] == GeneratedAppBuildStatus.QUEUED.value
    async with factory() as session:
        job = await session.get(GeneratedAppBuildJob, UUID(revision_result["build_job_id"]))
        assert job is not None
        assert job.base_revision_id is not None
        blueprint = job.request["blueprint"]
        assert blueprint["revision_request"].startswith("Make completed tasks")
        assert blueprint["base_revision"]["source_files"]["src/App.tsx"]
        assert blueprint["manifest"] == MANIFEST

    deleted = await DeleteCustomAppRecordTool(session_factory=factory).execute(
        context=context,
        arguments={
            "app_id": str(code_app.id),
            "record_id": record_id,
            "expected_version": 2,
        },
    )
    assert deleted["deleted_record_id"] == record_id
    await engine.dispose()


@pytest.mark.anyio
async def test_record_versions_are_database_compare_and_swap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, factory = await _database()
    owner, _, code_app = await _deployed_app(factory)

    async with factory() as session:
        actor = await authorize_user(session, app_id=code_app.id, user_id=owner.id)
        record = await create_data_record(
            session,
            app_id=code_app.id,
            actor=actor,
            entity="task",
            data={"title": "Ship it", "done": False},
            idempotency_key="record-create-cas",
        )

        async def change_version_before_compare(*_args, **_kwargs) -> None:
            await session.execute(
                update(type(record))
                .where(type(record).id == record.id)
                .values(
                    data={"title": "Changed elsewhere", "done": True},
                    version=type(record).version + 1,
                )
                .execution_options(synchronize_session=False)
            )

        monkeypatch.setattr(
            generated_apps_v2,
            "_enforce_mutation_rate_limit",
            change_version_before_compare,
        )
        with pytest.raises(CodeAppConflictError, match="changed by someone else"):
            await update_data_record(
                session,
                app_id=code_app.id,
                record_id=record.id,
                actor=actor,
                expected_version=1,
                data={"title": "Overwrite", "done": True},
                idempotency_key="record-update-cas",
            )
        await session.rollback()

    async with factory() as session:
        actor = AppActor(role=GeneratedAppRole.OWNER.value, user_id=owner.id)
        record = await session.scalar(
            select(GeneratedAppDataRecord).where(
                GeneratedAppDataRecord.app_id == code_app.id
            )
        )
        assert record is not None
        record_id = record.id

        async def change_version_before_delete(*_args, **_kwargs) -> None:
            await session.execute(
                update(type(record))
                .where(type(record).id == record.id)
                .values(version=type(record).version + 1)
                .execution_options(synchronize_session=False)
            )

        monkeypatch.setattr(
            generated_apps_v2,
            "_enforce_mutation_rate_limit",
            change_version_before_delete,
        )
        with pytest.raises(CodeAppConflictError, match="changed by someone else"):
            await delete_data_record(
                session,
                app_id=code_app.id,
                record_id=record.id,
                actor=actor,
                expected_version=1,
                idempotency_key="record-delete-cas",
            )
        await session.rollback()
        assert await session.get(GeneratedAppDataRecord, record_id) is not None

    await engine.dispose()


@pytest.mark.anyio
async def test_revision_queue_is_single_flight_and_stale_build_cannot_promote() -> None:
    engine, factory = await _database()
    owner, _, code_app = await _deployed_app(factory)

    async with factory() as session:
        deployment = await session.get(GeneratedAppDeployment, code_app.id)
        assert deployment is not None
        base_revision_id = deployment.active_revision_id
        job = await queue_code_app_revision(
            session,
            user_id=owner.id,
            app_id=code_app.id,
            request={"blueprint": {"title": "Revision one"}},
        )
        assert job.base_revision_id == base_revision_id
        job_id = job.id
        with pytest.raises(CodeAppConflictError, match="revision in progress"):
            await queue_code_app_revision(
                session,
                user_id=owner.id,
                app_id=code_app.id,
                request={"blueprint": {"title": "Revision two"}},
            )
        await session.rollback()

    async with factory() as session:
        deployment = await session.get(GeneratedAppDeployment, code_app.id)
        assert deployment is not None
        manual_revision = GeneratedAppRevision(
            app_id=code_app.id,
            revision_number=2,
            title="Manual revision",
            description="",
            manifest=MANIFEST,
            seed_data={},
            source_files={"src/App.tsx": "export default function App() { return 'manual' }"},
            artifact={},
            artifact_url="artifact://manual",
            artifact_sha256="c" * 64,
            sdk_version="1.0.0",
            dependency_lock={},
            test_results={"passed": True},
        )
        session.add(manual_revision)
        await session.flush()
        deployment.previous_revision_id = deployment.active_revision_id
        deployment.active_revision_id = manual_revision.id
        deployment.deployment_version += 1
        await session.commit()
        manual_revision_id = manual_revision.id

    async with factory() as session:
        claim = await claim_next_build(session, worker_id="stale-builder")
        assert claim is not None and claim.job_id == job_id
        assert claim.base_revision_id == base_revision_id
        with pytest.raises(CodeAppStaleBuildError, match="stale build was not deployed"):
            await complete_build(
                session,
                job_id=claim.job_id,
                worker_id="stale-builder",
                expected_attempt=claim.attempt,
                manifest=MANIFEST,
                source_files={"src/App.tsx": "export default function App() { return 'stale' }"},
                artifact={},
                artifact_url="artifact://stale",
                artifact_sha256="d" * 64,
                sdk_version="1.0.0",
            )
        await session.rollback()

    async with factory() as session:
        deployment = await session.get(GeneratedAppDeployment, code_app.id)
        assert deployment is not None
        assert deployment.active_revision_id == manual_revision_id
        assert (
            await session.scalar(
                select(GeneratedAppRevision).where(
                    GeneratedAppRevision.app_id == code_app.id,
                    GeneratedAppRevision.artifact_url == "artifact://stale",
                )
            )
            is None
        )

    await engine.dispose()


@pytest.mark.anyio
async def test_code_app_controls_reject_non_owner_and_group_context() -> None:
    engine, factory = await _database()
    owner, direct, code_app = await _deployed_app(factory)
    async with factory() as session:
        other = User(phone_number="+14155550102")
        session.add(other)
        await session.flush()
        other_direct = Conversation(user_id=other.id)
        group = Conversation(user_id=owner.id, kind=ConversationKind.GROUP.value)
        session.add_all([other_direct, group])
        session.add(
            GeneratedAppMembership(
                app_id=code_app.id,
                user_id=other.id,
                role=GeneratedAppRole.EDITOR.value,
            )
        )
        await session.commit()

    tool = InspectCustomAppTool(Settings(), session_factory=factory)
    with pytest.raises(CodeAppAuthorizationError):
        await tool.execute(
            context=ToolContext(user_id=other.id, conversation_id=other_direct.id),
            arguments={"app_id": str(code_app.id)},
        )
    with pytest.raises(ValueError, match="direct chat"):
        await tool.execute(
            context=ToolContext(user_id=owner.id, conversation_id=group.id),
            arguments={"app_id": str(code_app.id)},
        )
    link_tool = CreateCustomAppLinkTool(Settings(), session_factory=factory)
    with pytest.raises(CodeAppAuthorizationError):
        await link_tool.execute(
            context=ToolContext(user_id=other.id, conversation_id=other_direct.id),
            arguments={"app_id": str(code_app.id)},
        )
    with pytest.raises(ValueError, match="direct chat"):
        await link_tool.execute(
            context=ToolContext(user_id=owner.id, conversation_id=group.id),
            arguments={"app_id": str(code_app.id)},
        )
    async with factory() as session:
        membership = await session.scalar(
            select(GeneratedAppMembership).where(
                GeneratedAppMembership.app_id == code_app.id,
                GeneratedAppMembership.user_id == other.id,
            )
        )
        assert membership is not None
        membership.role = GeneratedAppRole.OWNER.value
        await session.commit()
    rollback = RollbackCustomAppTool(Settings(), session_factory=factory)
    with pytest.raises(CodeAppAuthorizationError):
        await rollback.execute(
            context=ToolContext(user_id=other.id, conversation_id=other_direct.id),
            arguments={
                "app_id": str(code_app.id),
                "expected_active_revision_id": str(uuid4()),
            },
        )
    with pytest.raises(ValueError, match="direct chat"):
        await rollback.execute(
            context=ToolContext(user_id=owner.id, conversation_id=group.id),
            arguments={
                "app_id": str(code_app.id),
                "expected_active_revision_id": str(uuid4()),
            },
        )
    await engine.dispose()


@pytest.mark.anyio
async def test_owner_can_reversibly_roll_back_previous_deployed_revision() -> None:
    engine, factory = await _database()
    owner, direct, code_app = await _deployed_app(factory)
    settings = Settings(generated_app_public_url="https://app.textdot.test")
    context = ToolContext(user_id=owner.id, conversation_id=direct.id)

    queued = await ReviseCustomAppTool(session_factory=factory).execute(
        context=context,
        arguments={
            "app_id": str(code_app.id),
            "change_request": "Make the progress view more compact.",
            "title": None,
            "description": None,
            "visual_direction": None,
            "manifest_json": None,
            "seed_data_json": None,
        },
    )
    async with factory() as session:
        first_revision = await session.scalar(
            select(GeneratedAppRevision).where(GeneratedAppRevision.app_id == code_app.id)
        )
        claim = await claim_next_build(session, worker_id="revision-builder")
        assert claim is not None
        second_revision = await complete_build(
            session,
            job_id=claim.job_id,
            worker_id="revision-builder",
            expected_attempt=claim.attempt,
            manifest=MANIFEST,
            source_files={"src/App.tsx": "export default function App() { return 'v2' }"},
            artifact={"render_document": {"schema_version": 1, "root": {"type": "page"}}},
            artifact_url="artifact://launch-list-v2",
            artifact_sha256="b" * 64,
            sdk_version="1.0.0",
        )
    assert queued["build_job_id"] == str(claim.job_id)
    assert first_revision is not None
    assert second_revision.revision_number == 2

    inspected = await InspectCustomAppTool(settings, session_factory=factory).execute(
        context=context,
        arguments={"app_id": str(code_app.id)},
    )
    assert inspected["rollback_available"] is True
    assert inspected["previous_revision_id"] == str(first_revision.id)

    rollback = RollbackCustomAppTool(settings, session_factory=factory)
    restored = await rollback.execute(
        context=context,
        arguments={
            "app_id": str(code_app.id),
            "expected_active_revision_id": str(second_revision.id),
        },
    )
    assert restored == {
        "app_id": str(code_app.id),
        "title": "Launch list",
        "app_url": f"https://app.textdot.test/a/{code_app.public_id}",
        "active_revision_id": str(first_revision.id),
        "active_revision_number": 1,
        "deployment_version": 3,
        "rollback_is_reversible": True,
    }
    with pytest.raises(CodeAppConflictError, match="changed since it was inspected"):
        await rollback.execute(
            context=context,
            arguments={
                "app_id": str(code_app.id),
                "expected_active_revision_id": str(second_revision.id),
            },
        )

    reapplied = await rollback.execute(
        context=context,
        arguments={
            "app_id": str(code_app.id),
            "expected_active_revision_id": str(first_revision.id),
        },
    )
    assert reapplied["active_revision_id"] == str(second_revision.id)
    assert reapplied["active_revision_number"] == 2
    assert reapplied["deployment_version"] == 4
    assert reapplied["app_url"] == restored["app_url"]

    async with factory() as session:
        deployment = await session.get(GeneratedAppDeployment, code_app.id)
        refreshed_app = await session.get(type(code_app), code_app.id)
        events = list(
            (
                await session.scalars(
                    select(GeneratedAppEvent)
                    .where(
                        GeneratedAppEvent.app_id == code_app.id,
                        GeneratedAppEvent.event_type == "app.deployment.rolled_back",
                    )
                    .order_by(GeneratedAppEvent.created_at)
                )
            ).all()
        )
        assert deployment is not None
        assert deployment.active_revision_id == second_revision.id
        assert deployment.previous_revision_id == first_revision.id
        assert refreshed_app is not None and refreshed_app.current_version == 2
        assert [event.payload["deployment_version"] for event in events] == [3, 4]
        assert [event.idempotency_key for event in events] == [
            "app-rollback:3",
            "app-rollback:4",
        ]

    pending = await ReviseCustomAppTool(session_factory=factory).execute(
        context=context,
        arguments={
            "app_id": str(code_app.id),
            "change_request": "Try a warmer visual direction.",
            "title": None,
            "description": None,
            "visual_direction": "Warm and tactile.",
            "manifest_json": None,
            "seed_data_json": None,
        },
    )
    assert pending["status"] == GeneratedAppBuildStatus.QUEUED.value
    with pytest.raises(CodeAppConflictError, match="revision in progress"):
        await rollback.execute(
            context=context,
            arguments={
                "app_id": str(code_app.id),
                "expected_active_revision_id": str(second_revision.id),
            },
        )
    await engine.dispose()


@pytest.mark.anyio
async def test_group_apps_are_collaborative_and_retryable_builds_requeue_safely() -> None:
    engine, factory = await _database()
    async with factory() as session:
        owner = User(phone_number="+14155550103")
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
        code_app, _, _ = await create_code_app_build(
            session,
            user_id=owner.id,
            conversation_id=group.id,
            title="Trip split",
            description="Shared costs",
            request={"blueprint": {"title": "Trip split", "manifest": MANIFEST, "seed_data": {}}},
        )
        ticket = await session.scalar(
            select(GeneratedAppAccessTicket).where(
                GeneratedAppAccessTicket.app_id == code_app.id
            )
        )
        assert code_app.access_mode == GeneratedAppAccessMode.COLLABORATIVE_LINK.value
        assert ticket is not None
        assert ticket.principal_user_id is None
        assert ticket.role == GeneratedAppRole.MEMBER.value
        expires_at = (
            ticket.expires_at.replace(tzinfo=UTC)
            if ticket.expires_at.tzinfo is None
            else ticket.expires_at
        )
        assert (expires_at - datetime.now(UTC)).total_seconds() > 6 * 86_400

    for expected_attempt in (1, 2):
        async with factory() as session:
            claim = await claim_next_build(session, worker_id=f"builder-{expected_attempt}")
            assert claim is not None and claim.attempt == expected_attempt
            retried = await fail_build(
                session,
                job_id=claim.job_id,
                worker_id=f"builder-{expected_attempt}",
                expected_attempt=claim.attempt,
                error='{"code":"timeout","retryable":true}',
                retryable=True,
            )
            assert retried.status == GeneratedAppBuildStatus.QUEUED.value
            assert await session.scalar(select(UserEvent)) is None

    async with factory() as session:
        claim = await claim_next_build(session, worker_id="builder-3")
        assert claim is not None and claim.attempt == 3
        exhausted = await fail_build(
            session,
            job_id=claim.job_id,
            worker_id="builder-3",
            expected_attempt=claim.attempt,
            error='{"code":"timeout","retryable":true}',
            retryable=True,
        )
        assert exhausted.status == GeneratedAppBuildStatus.FAILED.value
        event = await session.scalar(select(UserEvent))
        assert event is not None and event.event_type == "app.build.failed"
    await engine.dispose()


@pytest.mark.anyio
async def test_direct_chat_can_create_a_collaborative_app_with_anonymous_member_links() -> None:
    engine, factory = await _database()
    async with factory() as session:
        owner = User(phone_number="+14155550123")
        session.add(owner)
        await session.flush()
        conversation = Conversation(user_id=owner.id)
        session.add(conversation)
        await session.commit()

    settings = Settings(generated_app_public_url="https://app.textdot.test")
    context = ToolContext(user_id=owner.id, conversation_id=conversation.id)
    created = await CreateGeneratedAppTool(
        settings,
        session_factory=factory,
    ).execute(
        context=context,
        arguments={
            "title": "Shared tasks",
            "description": "A todo list other people can update",
            "purpose": "Finish shared work together",
            "product_brief": "A collaborative task list for a small group.",
            "visual_direction": "Friendly, compact, and mobile-first.",
            "access_mode": "collaborative_link",
            "entities": [
                {
                    "name": "task",
                    "description": "One shared task",
                    "fields": [
                        {"name": "title", "type": "string", "required": True},
                        {"name": "done", "type": "boolean", "required": True},
                    ],
                }
            ],
            "capabilities": [],
            "seed_data": "{}",
        },
    )
    assert created["access_mode"] == GeneratedAppAccessMode.COLLABORATIVE_LINK.value

    async with factory() as session:
        app = await session.get(GeneratedApp, UUID(created["app_id"]))
        job = await session.get(GeneratedAppBuildJob, UUID(created["build_job_id"]))
        assert app is not None and job is not None
        assert app.access_mode == GeneratedAppAccessMode.COLLABORATIVE_LINK.value
        initial_ticket = await session.scalar(
            select(GeneratedAppAccessTicket).where(
                GeneratedAppAccessTicket.app_id == app.id
            )
        )
        assert initial_ticket is not None
        assert initial_ticket.principal_user_id is None
        assert initial_ticket.role == GeneratedAppRole.MEMBER.value

    async with factory() as session:
        claim = await claim_next_build(session, worker_id="direct-collaborative-builder")
        assert claim is not None
        await complete_build(
            session,
            job_id=claim.job_id,
            worker_id="direct-collaborative-builder",
            expected_attempt=claim.attempt,
            manifest=MANIFEST,
            source_files={"src/App.tsx": "export default function App() { return null }"},
            artifact={},
            artifact_url="artifact://shared-tasks",
            artifact_sha256="c" * 64,
            sdk_version="1",
            handoff_base_url=f"https://app.textdot.test/a/{app.public_id}",
        )

    async with factory() as session:
        tickets = list(
            (
                await session.scalars(
                    select(GeneratedAppAccessTicket)
                    .where(GeneratedAppAccessTicket.app_id == app.id)
                    .order_by(GeneratedAppAccessTicket.created_at)
                )
            ).all()
        )
        assert len(tickets) == 2
        assert all(ticket.principal_user_id is None for ticket in tickets)
        assert all(ticket.role == GeneratedAppRole.MEMBER.value for ticket in tickets)
        event = await session.scalar(
            select(UserEvent).where(UserEvent.event_type == "app.build.completed")
        )
        assert event is not None
        completion_ticket = event.payload["app_url"].split("#handoff=", maxsplit=1)[1]
        _, first_app_session = await redeem_access_ticket(
            session,
            public_id=app.public_id,
            token=completion_ticket,
        )
        _, second_app_session = await redeem_access_ticket(
            session,
            public_id=app.public_id,
            token=completion_ticket,
        )
        assert first_app_session.id != second_app_session.id
        assert first_app_session.user_id is second_app_session.user_id is None
        assert first_app_session.role == second_app_session.role == GeneratedAppRole.MEMBER.value

    fresh_link = await CreateCustomAppLinkTool(
        settings,
        session_factory=factory,
    ).execute(context=context, arguments={"app_id": str(app.id)})
    assert fresh_link["private"] is False
    assert fresh_link["access_mode"] == GeneratedAppAccessMode.COLLABORATIVE_LINK.value
    async with factory() as session:
        fresh_ticket = await session.scalar(
            select(GeneratedAppAccessTicket)
            .where(GeneratedAppAccessTicket.app_id == app.id)
            .order_by(GeneratedAppAccessTicket.created_at.desc())
        )
        assert fresh_ticket is not None
        assert fresh_ticket.principal_user_id is None
        assert fresh_ticket.role == GeneratedAppRole.MEMBER.value
    await engine.dispose()


@pytest.mark.anyio
async def test_create_app_idempotency_distinguishes_access_mode() -> None:
    engine, factory = await _database()
    async with factory() as session:
        owner = User(phone_number="+14155550124")
        session.add(owner)
        await session.flush()
        conversation = Conversation(user_id=owner.id)
        session.add(conversation)
        await session.commit()
        await create_code_app_build(
            session,
            user_id=owner.id,
            conversation_id=conversation.id,
            title="Access-specific app",
            description="",
            request={"blueprint": {"title": "Access-specific app"}},
            access_mode=GeneratedAppAccessMode.PRIVATE_LINK.value,
            idempotency_key="access-specific-build",
        )
        with pytest.raises(CodeAppConflictError, match="different app build"):
            await create_code_app_build(
                session,
                user_id=owner.id,
                conversation_id=conversation.id,
                title="Access-specific app",
                description="",
                request={"blueprint": {"title": "Access-specific app"}},
                access_mode=GeneratedAppAccessMode.COLLABORATIVE_LINK.value,
                idempotency_key="access-specific-build",
            )
    await engine.dispose()


@pytest.mark.anyio
async def test_linked_group_member_creates_app_owned_by_canonical_group_owner() -> None:
    engine, factory = await _database()
    async with factory() as session:
        owner = User(phone_number="+14155550113")
        requester = User(phone_number="+14155550114")
        session.add_all([owner, requester])
        await session.flush()
        group = Conversation(user_id=owner.id, kind=ConversationKind.GROUP.value)
        session.add(group)
        await session.flush()
        session.add_all(
            [
                ConversationMember(
                    conversation_id=group.id,
                    user_id=owner.id,
                    external_handle=owner.phone_number,
                    role="owner",
                ),
                ConversationMember(
                    conversation_id=group.id,
                    user_id=requester.id,
                    external_handle=requester.phone_number,
                ),
            ]
        )
        await session.commit()

    created = await CreateGeneratedAppTool(
        Settings(generated_app_public_url="https://app.textdot.test"),
        session_factory=factory,
    ).execute(
        context=ToolContext(user_id=requester.id, conversation_id=group.id),
        arguments={
            "title": "Cottage split",
            "description": "Track shared cottage costs",
            "purpose": "Settle the trip fairly",
            "product_brief": "A shared expense ledger.",
            "visual_direction": "Warm, compact, and useful on phones.",
            "access_mode": "private_link",
            "entities": [
                {
                    "name": "expense",
                    "description": "One shared cost",
                    "fields": [
                        {"name": "title", "type": "string", "required": True},
                        {"name": "amount", "type": "number", "required": True},
                    ],
                }
            ],
            "capabilities": [],
            "seed_data": "{}",
        },
    )
    async with factory() as session:
        app = await session.get(GeneratedApp, UUID(created["app_id"]))
        assert app is not None
        assert app.user_id == owner.id
        assert app.conversation_id == group.id
        assert app.access_mode == GeneratedAppAccessMode.COLLABORATIVE_LINK.value
    await engine.dispose()


@pytest.mark.anyio
async def test_removed_group_requester_cannot_create_app_for_owner() -> None:
    engine, factory = await _database()
    async with factory() as session:
        owner = User(phone_number="+14155550115")
        requester = User(phone_number="+14155550116")
        session.add_all([owner, requester])
        await session.flush()
        group = Conversation(user_id=owner.id, kind=ConversationKind.GROUP.value)
        session.add(group)
        await session.flush()
        session.add_all(
            [
                ConversationMember(
                    conversation_id=group.id,
                    user_id=owner.id,
                    external_handle=owner.phone_number,
                    role="owner",
                ),
                ConversationMember(
                    conversation_id=group.id,
                    user_id=requester.id,
                    external_handle=requester.phone_number,
                    status="removed",
                ),
            ]
        )
        await session.commit()

        with pytest.raises(CodeAppAuthorizationError, match="requester"):
            await create_code_app_build(
                session,
                user_id=owner.id,
                requester_user_id=requester.id,
                conversation_id=group.id,
                title="Not allowed",
                description="",
                request={"blueprint": {"title": "Not allowed"}},
            )
    await engine.dispose()


def test_custom_app_tool_schemas_are_closed_and_use_json_strings() -> None:
    tools = [
        InspectCustomAppTool(Settings()),
        CreateCustomAppLinkTool(Settings()),
        ListCustomAppRecordsTool(),
        CreateCustomAppRecordTool(),
        UpdateCustomAppRecordTool(),
        DeleteCustomAppRecordTool(),
        ReviseCustomAppTool(),
        RollbackCustomAppTool(Settings()),
    ]

    def assert_closed(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node.get("additionalProperties") is False
                assert set(node.get("required", [])) == set(node.get("properties", {}))
            for child in node.values():
                assert_closed(child)
        elif isinstance(node, list):
            for child in node:
                assert_closed(child)

    for tool in tools:
        assert_closed(tool.definition.parameters)
    create_properties = CreateCustomAppRecordTool().definition.parameters["properties"]
    assert create_properties["data_json"]["type"] == "string"
    revision_properties = ReviseCustomAppTool().definition.parameters["properties"]
    assert revision_properties["manifest_json"]["type"] == ["string", "null"]
    assert revision_properties["seed_data_json"]["type"] == ["string", "null"]
