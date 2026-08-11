import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from benji_api.models.channel import Message, MessageDirection
from benji_api.models.finance import FinancialGoal, FinancialGoalStatus
from benji_api.models.generated_app import (
    GeneratedApp,
    GeneratedAppRecord,
    GeneratedAppStatus,
)
from benji_api.models.memory import MemoryFact, MemoryFactStatus
from benji_api.models.schedule import ScheduledTask, ScheduledTaskStatus


@dataclass(frozen=True, slots=True)
class RecentArtifact:
    title: str
    template: str
    created_at: datetime
    record_count: int
    last_activity_at: datetime | None


@dataclass(frozen=True, slots=True)
class ActiveCommitment:
    title: str
    kind: str
    cadence: str | None = None


@dataclass(frozen=True, slots=True)
class RelationshipState:
    previous_message_at: datetime | None = None
    recent_artifacts: tuple[RecentArtifact, ...] = ()
    active_commitments: tuple[ActiveCommitment, ...] = ()
    onboarding_handoff_pending: bool = False

    @property
    def empty(self) -> bool:
        return (
            not self.recent_artifacts
            and not self.active_commitments
            and not self.onboarding_handoff_pending
        )


async def load_relationship_state(
    session: AsyncSession,
    *,
    user_id: UUID,
    conversation_id: UUID,
    trigger: Message,
    artifact_limit: int = 3,
    now: datetime | None = None,
) -> RelationshipState:
    """Load compact, trusted relationship state rather than another transcript dump."""
    reference_time = now or trigger.created_at or datetime.now(UTC)
    reference_time = _aware_utc(reference_time)
    previous_message = await session.scalar(
        select(Message)
        .where(
            Message.conversation_id == conversation_id,
            Message.id != trigger.id,
            Message.created_at < trigger.created_at,
        )
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(1)
    )
    previous_message_at = previous_message.created_at if previous_message is not None else None
    onboarding_handoff_pending = bool(
        previous_message is not None
        and previous_message.direction == MessageDirection.OUTBOUND.value
        and previous_message.raw_payload.get("onboarding_completed") is True
    )

    cutoff = reference_time - timedelta(days=14)
    artifact_rows = (
        await session.execute(
            select(
                GeneratedApp,
                func.count(GeneratedAppRecord.id),
                func.max(GeneratedAppRecord.updated_at),
            )
            .outerjoin(GeneratedAppRecord, GeneratedAppRecord.app_id == GeneratedApp.id)
            .where(
                GeneratedApp.user_id == user_id,
                GeneratedApp.status == GeneratedAppStatus.ACTIVE.value,
                GeneratedApp.created_at >= cutoff,
            )
            .group_by(GeneratedApp.id)
            .order_by(GeneratedApp.created_at.desc())
            .limit(artifact_limit)
        )
    ).all()
    artifacts = tuple(
        RecentArtifact(
            title=app.title,
            template=app.template,
            created_at=_aware_utc(app.created_at),
            record_count=int(record_count or 0),
            last_activity_at=_aware_utc(last_activity_at) if last_activity_at else None,
        )
        for app, record_count, last_activity_at in artifact_rows
    )

    goals = (
        await session.scalars(
            select(FinancialGoal)
            .where(
                FinancialGoal.user_id == user_id,
                FinancialGoal.status == FinancialGoalStatus.ACTIVE.value,
            )
            .order_by(FinancialGoal.updated_at.desc())
            .limit(2)
        )
    ).all()
    schedules = (
        await session.scalars(
            select(ScheduledTask)
            .where(
                ScheduledTask.user_id == user_id,
                ScheduledTask.status == ScheduledTaskStatus.ACTIVE.value,
                ScheduledTask.action_type == "agent.reachout",
            )
            .order_by(ScheduledTask.updated_at.desc())
            .limit(2)
        )
    ).all()
    memory_commitments = (
        await session.scalars(
            select(MemoryFact)
            .where(
                MemoryFact.user_id == user_id,
                MemoryFact.status == MemoryFactStatus.ACTIVE.value,
                MemoryFact.kind.in_(("goal", "commitment")),
                MemoryFact.confidence >= 0.65,
                or_(
                    MemoryFact.valid_until.is_(None),
                    MemoryFact.valid_until > reference_time,
                ),
            )
            .order_by(MemoryFact.importance.desc(), MemoryFact.updated_at.desc())
            .limit(3)
        )
    ).all()
    commitments = _dedupe_commitments(
        [ActiveCommitment(title=goal.title, kind="financial_goal") for goal in goals]
        + [
            ActiveCommitment(
                title=task.title,
                kind="scheduled_reachout",
                cadence=task.recurrence,
            )
            for task in schedules
        ]
        + [
            ActiveCommitment(title=fact.statement, kind=fact.kind)
            for fact in memory_commitments
        ]
    )
    return RelationshipState(
        previous_message_at=(
            _aware_utc(previous_message_at) if previous_message_at is not None else None
        ),
        recent_artifacts=artifacts,
        active_commitments=commitments,
        onboarding_handoff_pending=onboarding_handoff_pending,
    )


def is_generic_opening(text: str) -> bool:
    normalized = " ".join(re.sub(r"[^a-z0-9' ]", " ", text.lower()).split())
    return bool(
        re.fullmatch(
            r"(?:hi+|hey+|hello+|yo+|yoo+|sup|what'?s up|wassup|wyd|"
            r"good (?:morning|afternoon|evening))",
            normalized,
        )
    )


def is_identity_question(text: str) -> bool:
    normalized = " ".join(re.sub(r"[^a-z0-9' ]", " ", text.lower()).split())
    return bool(
        re.fullmatch(
            r"(?:(?:so )?(?:who|what) (?:even )?(?:are you|is dot)"
            r"(?: basically| exactly)?|(?:so )?what do you (?:actually )?do|"
            r"(?:so )?how do you work)",
            normalized,
        )
    )


def is_social_acknowledgment(text: str) -> bool:
    normalized = " ".join(re.sub(r"[^a-z0-9' ]", " ", text.lower()).split())
    return bool(
        re.fullmatch(
            r"(?:ok(?:ay)?|cool|nice|great|sweet|sounds good|got it|fair|"
            r"no worries|all good|thanks|thank you|lol|lmao|haha+|yeah|yep|sure)",
            normalized,
        )
    )


def _dedupe_commitments(items: list[ActiveCommitment]) -> tuple[ActiveCommitment, ...]:
    result: list[ActiveCommitment] = []
    seen: set[str] = set()
    for item in items:
        key = " ".join(item.title.lower().split())
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(item)
    return tuple(result)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
