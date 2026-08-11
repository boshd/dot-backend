from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from benji_api.config import Settings
from benji_api.db.session import async_session_factory
from benji_api.models.channel import Conversation, ConversationChannel, ConversationKind
from benji_api.models.schedule import (
    ScheduledTask,
    ScheduledTaskRecurrence,
    ScheduledTaskStatus,
)
from benji_api.services.user_events import enqueue_user_event

logger = logging.getLogger(__name__)

AGENT_REACHOUT_ACTION = "agent.reachout"
FINANCIAL_SYNC_ACTION = "finance.sync"


class ScheduleValidationError(ValueError):
    pass


async def create_scheduled_task(
    session: AsyncSession,
    *,
    user_id: UUID,
    conversation_id: UUID | None,
    action_type: str,
    source: str,
    idempotency_key: str,
    title: str,
    payload: dict[str, Any],
    run_at: datetime,
    timezone: str = "UTC",
    recurrence: str = ScheduledTaskRecurrence.ONCE.value,
    delivery_provider: str | None = None,
) -> ScheduledTask:
    existing = await session.scalar(
        select(ScheduledTask).where(ScheduledTask.idempotency_key == idempotency_key)
    )
    if existing is not None:
        return existing
    run_at = _aware_utc(run_at)
    if run_at <= datetime.now(UTC) - timedelta(seconds=5):
        raise ScheduleValidationError("scheduled time must be in the future")
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError as error:
        raise ScheduleValidationError(f"unknown timezone: {timezone}") from error
    try:
        recurrence_value = ScheduledTaskRecurrence(recurrence).value
    except ValueError as error:
        raise ScheduleValidationError("recurrence must be once, daily, or weekly") from error
    if not title.strip():
        raise ScheduleValidationError("schedule title is required")
    task = ScheduledTask(
        user_id=user_id,
        conversation_id=conversation_id,
        action_type=action_type,
        source=source,
        idempotency_key=idempotency_key,
        title=title.strip()[:160],
        payload=payload,
        delivery_provider=delivery_provider,
        timezone=timezone,
        recurrence=recurrence_value,
        scheduled_for=run_at,
        next_attempt_at=run_at,
    )
    session.add(task)
    await session.flush()
    return task


async def preferred_delivery_provider(
    session: AsyncSession,
    *,
    conversation_id: UUID,
) -> str | None:
    conversation = await session.get(Conversation, conversation_id)
    if conversation is None or conversation.kind != ConversationKind.DIRECT.value:
        return None
    channel = await session.scalar(
        select(ConversationChannel)
        .where(
            ConversationChannel.conversation_id == conversation_id,
            ConversationChannel.provider == "linq",
            ConversationChannel.status == "active",
        )
        .order_by(ConversationChannel.updated_at.desc())
        .limit(1)
    )
    return "linq" if channel is not None else None


async def list_scheduled_tasks(
    session: AsyncSession,
    *,
    user_id: UUID,
    conversation_id: UUID | None = None,
    action_type: str | None = None,
    active_only: bool = True,
) -> list[ScheduledTask]:
    statement = select(ScheduledTask).where(ScheduledTask.user_id == user_id)
    if conversation_id is not None:
        statement = statement.where(ScheduledTask.conversation_id == conversation_id)
    if action_type is not None:
        statement = statement.where(ScheduledTask.action_type == action_type)
    if active_only:
        statement = statement.where(
            ScheduledTask.status.in_(
                (
                    ScheduledTaskStatus.ACTIVE.value,
                    ScheduledTaskStatus.PROCESSING.value,
                    ScheduledTaskStatus.FAILED.value,
                    ScheduledTaskStatus.PAUSED.value,
                )
            )
        )
    return list(
        (
            await session.scalars(
                statement.order_by(ScheduledTask.scheduled_for, ScheduledTask.created_at)
            )
        ).all()
    )


async def cancel_scheduled_task(
    session: AsyncSession,
    *,
    user_id: UUID,
    task_id: UUID,
) -> bool:
    task = await session.scalar(
        select(ScheduledTask).where(
            ScheduledTask.id == task_id,
            ScheduledTask.user_id == user_id,
        )
    )
    if task is None:
        return False
    if task.status in {
        ScheduledTaskStatus.COMPLETED.value,
        ScheduledTaskStatus.CANCELLED.value,
    }:
        return True
    task.status = ScheduledTaskStatus.CANCELLED.value
    task.cancelled_at = datetime.now(UTC)
    task.locked_at = None
    await session.flush()
    return True


async def dispatch_due_scheduled_task(
    *,
    settings: Settings,
    task_id: UUID | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> bool:
    factory = session_factory or async_session_factory
    task = await _claim_task(
        factory,
        max_attempts=settings.scheduled_task_max_attempts,
        task_id=task_id,
    )
    if task is None:
        return False
    try:
        if task.action_type == AGENT_REACHOUT_ACTION:
            await _dispatch_agent_reachout(factory, task)
        elif task.action_type == FINANCIAL_SYNC_ACTION:
            from benji_api.services.finance import sync_financial_connection

            raw_connection_id = task.payload.get("connection_id")
            if not isinstance(raw_connection_id, str):
                raise RuntimeError("financial sync task is missing connection_id")
            await sync_financial_connection(
                connection_id=UUID(raw_connection_id),
                settings=settings,
                notify_on_complete=bool(task.payload.get("notify_on_complete")),
                delivery_provider=task.delivery_provider,
                session_factory=factory,
            )
        else:
            raise RuntimeError(f"No scheduled action handler for {task.action_type}")
        await _complete_task(factory, task.id)
    except Exception as error:
        logger.exception("Scheduled task %s failed", task.id)
        await _retry_task(factory, task.id, error)
    return True


async def _dispatch_agent_reachout(
    factory: async_sessionmaker[AsyncSession],
    task: ScheduledTask,
) -> None:
    scheduled_for = _aware_utc(task.scheduled_for)
    async with factory() as session:
        await enqueue_user_event(
            session,
            user_id=task.user_id,
            conversation_id=task.conversation_id,
            event_type="schedule.triggered",
            source=task.source,
            idempotency_key=f"schedule.triggered:{task.id}:{scheduled_for.isoformat()}",
            payload={
                "schedule_id": str(task.id),
                "title": task.title,
                "goal": task.payload.get("goal"),
                "scheduled_for": scheduled_for.isoformat(),
                "timezone": task.timezone,
                "recurrence": task.recurrence,
                "run_count": task.run_count + 1,
            },
            delivery_provider=task.delivery_provider,
        )
        await session.commit()


async def _claim_task(
    factory: async_sessionmaker[AsyncSession],
    *,
    max_attempts: int,
    task_id: UUID | None,
) -> ScheduledTask | None:
    now = datetime.now(UTC)
    stale_before = now - timedelta(minutes=5)
    eligible = or_(
        and_(
            ScheduledTask.status == ScheduledTaskStatus.ACTIVE.value,
            ScheduledTask.next_attempt_at <= now,
        ),
        and_(
            ScheduledTask.status == ScheduledTaskStatus.FAILED.value,
            ScheduledTask.next_attempt_at <= now,
        ),
        and_(
            ScheduledTask.status == ScheduledTaskStatus.PROCESSING.value,
            ScheduledTask.locked_at <= stale_before,
        ),
    )
    async with factory() as session:
        statement = (
            select(ScheduledTask)
            .where(eligible, ScheduledTask.attempts < max_attempts)
            .order_by(ScheduledTask.next_attempt_at, ScheduledTask.created_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if task_id is not None:
            statement = statement.where(ScheduledTask.id == task_id)
        task = await session.scalar(statement)
        if task is None:
            return None
        task.status = ScheduledTaskStatus.PROCESSING.value
        task.attempts += 1
        task.locked_at = now
        task.error = None
        await session.commit()
        return task


async def _complete_task(
    factory: async_sessionmaker[AsyncSession],
    task_id: UUID,
) -> None:
    now = datetime.now(UTC)
    async with factory() as session:
        task = await session.get(ScheduledTask, task_id)
        if task is None or task.status == ScheduledTaskStatus.CANCELLED.value:
            return
        task.run_count += 1
        task.attempts = 0
        task.locked_at = None
        task.last_run_at = now
        task.error = None
        if task.recurrence == ScheduledTaskRecurrence.ONCE.value:
            task.status = ScheduledTaskStatus.COMPLETED.value
            task.completed_at = now
        else:
            next_run = _next_recurring_run(task, after=now)
            task.scheduled_for = next_run
            task.next_attempt_at = next_run
            task.status = ScheduledTaskStatus.ACTIVE.value
        await session.commit()


async def _retry_task(
    factory: async_sessionmaker[AsyncSession],
    task_id: UUID,
    error: Exception,
) -> None:
    async with factory() as session:
        task = await session.get(ScheduledTask, task_id)
        if task is None or task.status == ScheduledTaskStatus.CANCELLED.value:
            return
        delay_seconds = min(2 ** max(task.attempts, 1), 300)
        task.status = ScheduledTaskStatus.FAILED.value
        task.error = str(error)[:2_000]
        task.locked_at = None
        task.next_attempt_at = datetime.now(UTC) + timedelta(seconds=delay_seconds)
        await session.commit()


def _next_recurring_run(task: ScheduledTask, *, after: datetime) -> datetime:
    timezone = ZoneInfo(task.timezone)
    candidate = _aware_utc(task.scheduled_for).astimezone(timezone)
    step = timedelta(days=1 if task.recurrence == ScheduledTaskRecurrence.DAILY.value else 7)
    after_local = _aware_utc(after).astimezone(timezone)
    while candidate <= after_local:
        next_date = candidate.date() + step
        candidate = datetime.combine(next_date, candidate.timetz(), tzinfo=timezone)
    return candidate.astimezone(UTC)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
