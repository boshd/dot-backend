import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from generated_app_artifacts import compiled_artifact
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from benji_api.agents.tools import (
    CreateCustomAppRecordTool,
    CreateGeneratedAppTool,
    DeleteCustomAppRecordTool,
    ReviseCustomAppTool,
    RollbackCustomAppTool,
    ToolRegistry,
    UpdateCustomAppRecordTool,
)
from benji_api.agents.types import ToolContext, ToolDefinition
from benji_api.config import Settings
from benji_api.db.base import Base
from benji_api.models.agent import AgentRun, AgentToolCall, ToolCallStatus
from benji_api.models.channel import Conversation
from benji_api.models.generated_app import GeneratedApp
from benji_api.models.generated_app_v2 import (
    GeneratedAppBuildJob,
    GeneratedAppDataRecord,
    GeneratedAppDeployment,
    GeneratedAppEvent,
)
from benji_api.models.user import User
from benji_api.services.generated_apps_v2 import (
    CodeAppConflictError,
    claim_next_build,
    complete_build,
    create_code_app_build,
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


async def _database(*, path: Path | None = None):
    if path is None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    else:
        engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, factory


async def _identity(factory):
    async with factory() as session:
        owner = User(phone_number="+14155550191")
        session.add(owner)
        await session.flush()
        direct = Conversation(user_id=owner.id)
        session.add(direct)
        await session.flush()
        run = AgentRun(
            conversation_id=direct.id,
            user_id=owner.id,
            provider="test",
            model="test",
        )
        session.add(run)
        await session.commit()
    return owner, direct, run


async def _deployed_app(factory):
    owner, direct, run = await _identity(factory)
    async with factory() as session:
        app, _, _ = await create_code_app_build(
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
        revision = await complete_build(
            session,
            job_id=claim.job_id,
            worker_id="builder",
            expected_attempt=claim.attempt,
            manifest=MANIFEST,
            source_files={"src/App.tsx": "export default function App() { return null }"},
            artifact=compiled_artifact(),
            artifact_url="artifact://launch-list",
            artifact_sha256="a" * 64,
            sdk_version="1.0.0",
        )
    return owner, direct, run, app, revision


async def _expired_journal(
    factory,
    *,
    context: ToolContext,
    tool_name: str,
    arguments: dict[str, Any],
) -> None:
    assert context.agent_run_id is not None and context.tool_call_id is not None
    async with factory() as session:
        session.add(
            AgentToolCall(
                agent_run_id=context.agent_run_id,
                external_call_id=context.tool_call_id,
                tool_name=tool_name,
                arguments=arguments,
                output={},
                status=ToolCallStatus.RUNNING.value,
                attempts=1,
                claimed_by="crashed-worker",
                lease_expires_at=datetime.now(UTC) - timedelta(seconds=1),
            )
        )
        await session.commit()


@pytest.mark.anyio
async def test_create_app_replays_after_domain_commit_before_journal_settlement() -> None:
    engine, factory = await _database()
    owner, direct, run = await _identity(factory)
    settings = Settings(generated_app_public_url="https://app.textdot.test")
    context = ToolContext(
        user_id=owner.id,
        conversation_id=direct.id,
        agent_run_id=run.id,
        tool_call_id="call-create",
    )
    arguments = {
        "title": "Trip split",
        "description": "Split a trip fairly",
        "purpose": "Track shared trip expenses",
        "product_brief": "Add expenses and show balances.",
        "visual_direction": "Warm, compact, receipt-inspired.",
        "access_mode": "private_link",
        "entities": [
            {
                "name": "expense",
                "description": "Shared expense",
                "fields": [
                    {"name": "title", "type": "string", "required": True},
                    {"name": "amount", "type": "number", "required": True},
                ],
            }
        ],
        "capabilities": [],
        "seed_data": "{}",
    }
    tool = CreateGeneratedAppTool(settings, session_factory=factory)

    committed = await tool.execute(context=context, arguments=arguments)
    with pytest.raises(CodeAppConflictError, match="different app build"):
        await tool.execute(
            context=context,
            arguments={**arguments, "title": "A different app"},
        )
    with pytest.raises(CodeAppConflictError, match="different app build"):
        await tool.execute(
            context=context,
            arguments={**arguments, "access_mode": "collaborative_link"},
        )
    await _expired_journal(
        factory,
        context=context,
        tool_name="create_personal_app",
        arguments=arguments,
    )
    replay, succeeded = await ToolRegistry(
        [tool], session_factory=factory
    ).execute(
        name="create_personal_app",
        context=context,
        arguments=arguments,
    )

    assert succeeded is True
    assert replay == {"ok": True, "result": committed}
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(GeneratedApp)) == 1
        assert await session.scalar(select(func.count()).select_from(GeneratedAppBuildJob)) == 1
        journal = await session.scalar(select(AgentToolCall))
        assert journal is not None
        assert journal.status == ToolCallStatus.COMPLETED.value
        assert journal.attempts == 2
        assert journal.claimed_by is None
        assert journal.lease_expires_at is None
    await engine.dispose()


@pytest.mark.anyio
async def test_record_tools_replay_crash_gap_without_duplicate_mutations() -> None:
    engine, factory = await _database()
    owner, direct, run, app, _ = await _deployed_app(factory)
    base_context = ToolContext(
        user_id=owner.id,
        conversation_id=direct.id,
        agent_run_id=run.id,
    )
    create_arguments = {
        "app_id": str(app.id),
        "entity": "task",
        "data_json": '{"title":"Ship it","done":false}',
    }
    create_context = replace(base_context, tool_call_id="call-record-create")
    create_tool = CreateCustomAppRecordTool(session_factory=factory)
    created = await create_tool.execute(context=create_context, arguments=create_arguments)
    await _expired_journal(
        factory,
        context=create_context,
        tool_name="add_custom_app_record",
        arguments=create_arguments,
    )
    replay, succeeded = await ToolRegistry(
        [create_tool], session_factory=factory
    ).execute(
        name="add_custom_app_record",
        context=create_context,
        arguments=create_arguments,
    )
    assert succeeded is True and replay == {"ok": True, "result": created}

    record_id = created["record"]["record_id"]
    update_arguments = {
        "app_id": str(app.id),
        "record_id": record_id,
        "expected_version": 1,
        "data_json": '{"title":"Ship it","done":true}',
    }
    update_context = replace(base_context, tool_call_id="call-record-update")
    update_tool = UpdateCustomAppRecordTool(session_factory=factory)
    updated = await update_tool.execute(context=update_context, arguments=update_arguments)
    await _expired_journal(
        factory,
        context=update_context,
        tool_name="update_custom_app_record",
        arguments=update_arguments,
    )
    replay, succeeded = await ToolRegistry(
        [update_tool], session_factory=factory
    ).execute(
        name="update_custom_app_record",
        context=update_context,
        arguments=update_arguments,
    )
    assert succeeded is True and replay == {"ok": True, "result": updated}

    delete_arguments = {
        "app_id": str(app.id),
        "record_id": record_id,
        "expected_version": 2,
    }
    delete_context = replace(base_context, tool_call_id="call-record-delete")
    delete_tool = DeleteCustomAppRecordTool(session_factory=factory)
    deleted = await delete_tool.execute(context=delete_context, arguments=delete_arguments)
    await _expired_journal(
        factory,
        context=delete_context,
        tool_name="delete_custom_app_record",
        arguments=delete_arguments,
    )
    replay, succeeded = await ToolRegistry(
        [delete_tool], session_factory=factory
    ).execute(
        name="delete_custom_app_record",
        context=delete_context,
        arguments=delete_arguments,
    )
    assert succeeded is True and replay == {"ok": True, "result": deleted}

    async with factory() as session:
        assert (
            await session.scalar(select(func.count()).select_from(GeneratedAppDataRecord))
            == 0
        )
        events = list((await session.scalars(select(GeneratedAppEvent))).all())
        assert [event.event_type for event in events] == [
            "app.data.created",
            "app.data.updated",
            "app.data.deleted",
        ]
        assert len({event.idempotency_key for event in events}) == 3
    await engine.dispose()


@pytest.mark.anyio
async def test_revision_and_rollback_replay_crash_gap_without_repeating_action() -> None:
    engine, factory = await _database()
    owner, direct, run, app, first_revision = await _deployed_app(factory)
    settings = Settings(generated_app_public_url="https://app.textdot.test")
    base_context = ToolContext(
        user_id=owner.id,
        conversation_id=direct.id,
        agent_run_id=run.id,
    )
    revise_context = replace(base_context, tool_call_id="call-revise")
    revise_arguments = {
        "app_id": str(app.id),
        "change_request": "Make the board denser.",
        "title": None,
        "description": None,
        "visual_direction": None,
        "manifest_json": None,
        "seed_data_json": None,
    }
    revise_tool = ReviseCustomAppTool(session_factory=factory)
    queued = await revise_tool.execute(context=revise_context, arguments=revise_arguments)
    await _expired_journal(
        factory,
        context=revise_context,
        tool_name="revise_custom_app",
        arguments=revise_arguments,
    )
    replay, succeeded = await ToolRegistry(
        [revise_tool], session_factory=factory
    ).execute(
        name="revise_custom_app",
        context=revise_context,
        arguments=revise_arguments,
    )
    assert succeeded is True and replay == {"ok": True, "result": queued}

    async with factory() as session:
        claim = await claim_next_build(session, worker_id="revision-builder")
        assert claim is not None
        second_revision = await complete_build(
            session,
            job_id=claim.job_id,
            worker_id="revision-builder",
            expected_attempt=claim.attempt,
            manifest=MANIFEST,
            source_files={"src/App.tsx": "export default function App() { return 'v2' }"},
            artifact=compiled_artifact(),
            artifact_url="artifact://launch-list-v2",
            artifact_sha256="b" * 64,
            sdk_version="1.0.0",
        )

    rollback_context = replace(base_context, tool_call_id="call-rollback")
    rollback_arguments = {
        "app_id": str(app.id),
        "expected_active_revision_id": str(second_revision.id),
    }
    rollback_tool = RollbackCustomAppTool(settings, session_factory=factory)
    restored = await rollback_tool.execute(
        context=rollback_context,
        arguments=rollback_arguments,
    )
    await _expired_journal(
        factory,
        context=rollback_context,
        tool_name="rollback_custom_app",
        arguments=rollback_arguments,
    )
    replay, succeeded = await ToolRegistry(
        [rollback_tool], session_factory=factory
    ).execute(
        name="rollback_custom_app",
        context=rollback_context,
        arguments=rollback_arguments,
    )
    assert succeeded is True and replay == {"ok": True, "result": restored}

    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(GeneratedAppBuildJob)) == 2
        deployment = await session.get(GeneratedAppDeployment, app.id)
        assert deployment is not None
        assert deployment.active_revision_id == first_revision.id
        rollback_events = list(
            (
                await session.scalars(
                    select(GeneratedAppEvent).where(
                        GeneratedAppEvent.event_type == "app.deployment.rolled_back"
                    )
                )
            ).all()
        )
        assert len(rollback_events) == 1
    await engine.dispose()


class _SlowMutationTool:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.executions = 0

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="add_custom_app_record",
            description="test mutation",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
        )

    async def execute(self, *, context: ToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
        del context, arguments
        self.executions += 1
        self.started.set()
        await self.release.wait()
        return {"executions": self.executions}


@pytest.mark.anyio
async def test_concurrent_running_journal_waits_and_replays_first_result(tmp_path: Path) -> None:
    engine, factory = await _database(path=tmp_path / "journal.sqlite3")
    owner, direct, run = await _identity(factory)
    context = ToolContext(
        user_id=owner.id,
        conversation_id=direct.id,
        agent_run_id=run.id,
        tool_call_id="call-concurrent",
    )
    tool = _SlowMutationTool()
    registry = ToolRegistry([tool], session_factory=factory)

    first = asyncio.create_task(
        registry.execute(name="add_custom_app_record", context=context, arguments={})
    )
    await tool.started.wait()
    second = asyncio.create_task(
        registry.execute(name="add_custom_app_record", context=context, arguments={})
    )
    await asyncio.sleep(0.05)
    assert tool.executions == 1
    tool.release.set()

    first_result, second_result = await asyncio.gather(first, second)
    assert first_result == second_result == (
        {"ok": True, "result": {"executions": 1}},
        True,
    )
    assert tool.executions == 1
    await engine.dispose()
