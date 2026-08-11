from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from benji_api.agents.tools import (
    CancelScheduledReachoutTool,
    ScheduleReachoutTool,
)
from benji_api.agents.types import ToolContext
from benji_api.config import Settings
from benji_api.db.base import Base
from benji_api.models import (
    Conversation,
    ConversationChannel,
    ScheduledTask,
    ScheduledTaskStatus,
    User,
    UserEvent,
)
from benji_api.services.schedules import dispatch_due_scheduled_task


@pytest.mark.anyio
async def test_recurring_reachout_becomes_a_durable_agent_event() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with session_factory() as session:
        user = User(phone_number="+14155552671")
        session.add(user)
        await session.flush()
        conversation = Conversation(user_id=user.id)
        session.add(conversation)
        await session.flush()
        session.add(
            ConversationChannel(
                conversation_id=conversation.id,
                provider="linq",
                external_id="chat-1",
                status="active",
            )
        )
        await session.commit()

    tool = ScheduleReachoutTool(session_factory=session_factory)
    created = await tool.execute(
        context=ToolContext(user_id=user.id, conversation_id=conversation.id),
        arguments={
            "title": "weekly savings review",
            "goal": "check whether i am still on pace for the november savings goal",
            "run_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
            "timezone": "Africa/Cairo",
            "recurrence": "weekly",
        },
    )

    async with session_factory() as session:
        task = await session.get(ScheduledTask, UUID(created["schedule_id"]))
        assert task is not None
        assert task.delivery_provider == "linq"
        task.scheduled_for = datetime.now(UTC) - timedelta(seconds=1)
        task.next_attempt_at = task.scheduled_for
        first_scheduled_for = task.scheduled_for
        await session.commit()

    handled = await dispatch_due_scheduled_task(
        settings=Settings(),
        task_id=task.id,
        session_factory=session_factory,
    )

    assert handled is True
    async with session_factory() as session:
        stored = await session.get(ScheduledTask, task.id)
        event = await session.scalar(select(UserEvent))
        assert stored is not None
        assert stored.status == ScheduledTaskStatus.ACTIVE.value
        assert stored.run_count == 1
        stored_scheduled_for = (
            stored.scheduled_for
            if stored.scheduled_for.tzinfo is not None
            else stored.scheduled_for.replace(tzinfo=UTC)
        )
        assert stored_scheduled_for > first_scheduled_for + timedelta(days=6)
        assert event is not None
        assert event.event_type == "schedule.triggered"
        assert event.delivery_provider == "linq"
        assert "november savings goal" in str(event.payload["goal"])

    cancel_tool = CancelScheduledReachoutTool(session_factory=session_factory)
    result = await cancel_tool.execute(
        context=ToolContext(user_id=user.id, conversation_id=conversation.id),
        arguments={"schedule_id": str(task.id)},
    )
    assert result["cancelled"] is True
    async with session_factory() as session:
        stored = await session.get(ScheduledTask, task.id)
        assert stored is not None
        assert stored.status == ScheduledTaskStatus.CANCELLED.value
    await engine.dispose()
