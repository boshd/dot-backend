from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from benji_api.models.finance import FinancialGoal, FinancialGoalStatus
from benji_api.services.schedules import (
    AGENT_REACHOUT_ACTION,
    cancel_scheduled_task,
    create_scheduled_task,
    preferred_delivery_provider,
)


async def create_financial_goal(
    session: AsyncSession,
    *,
    user_id: UUID,
    conversation_id: UUID,
    title: str,
    target_amount: Decimal,
    currency: str,
    target_date: date,
    baseline_amount: Decimal | None,
    proactive_checkins: bool,
    first_check_at: datetime | None,
    timezone: str | None,
) -> FinancialGoal:
    if target_amount <= 0:
        raise ValueError("target_amount must be positive")
    if target_date <= datetime.now(UTC).date():
        raise ValueError("target_date must be in the future")
    normalized_currency = currency.strip().upper()
    if not 3 <= len(normalized_currency) <= 16:
        raise ValueError("currency must be a valid currency code")
    if proactive_checkins and (first_check_at is None or timezone is None):
        raise ValueError("first_check_at and timezone are required for proactive check-ins")
    goal = FinancialGoal(
        user_id=user_id,
        conversation_id=conversation_id,
        title=title.strip()[:160],
        target_amount=target_amount,
        currency=normalized_currency,
        target_date=target_date,
        baseline_amount=baseline_amount,
    )
    session.add(goal)
    await session.flush()
    if proactive_checkins and first_check_at is not None and timezone is not None:
        delivery_provider = await preferred_delivery_provider(
            session, conversation_id=conversation_id
        )
        task = await create_scheduled_task(
            session,
            user_id=user_id,
            conversation_id=conversation_id,
            action_type=AGENT_REACHOUT_ACTION,
            source="financial_goal",
            idempotency_key=f"financial-goal:{goal.id}:weekly-review",
            title=f"Review {goal.title}",
            payload={
                "goal": (
                    f"Review financial goal {goal.id}: {goal.title}, target "
                    f"{goal.target_amount} {goal.currency} by {goal.target_date.isoformat()}. "
                    "Check current financial data, explain meaningful progress or drift, and only "
                    "message if there is a useful observation or next step."
                )
            },
            run_at=first_check_at,
            timezone=timezone,
            recurrence="weekly",
            delivery_provider=delivery_provider,
        )
        goal.schedule_id = task.id
    await session.flush()
    return goal


async def list_financial_goals(
    session: AsyncSession,
    *,
    user_id: UUID,
    active_only: bool = True,
) -> list[FinancialGoal]:
    statement = select(FinancialGoal).where(FinancialGoal.user_id == user_id)
    if active_only:
        statement = statement.where(FinancialGoal.status == FinancialGoalStatus.ACTIVE.value)
    return list(
        (
            await session.scalars(
                statement.order_by(FinancialGoal.target_date, FinancialGoal.created_at)
            )
        ).all()
    )


async def cancel_financial_goal(
    session: AsyncSession,
    *,
    user_id: UUID,
    goal_id: UUID,
) -> bool:
    goal = await session.scalar(
        select(FinancialGoal).where(
            FinancialGoal.id == goal_id,
            FinancialGoal.user_id == user_id,
        )
    )
    if goal is None:
        return False
    goal.status = FinancialGoalStatus.CANCELLED.value
    if goal.schedule_id is not None:
        await cancel_scheduled_task(
            session,
            user_id=user_id,
            task_id=goal.schedule_id,
        )
    await session.flush()
    return True
