from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from benji_api.agents.conversation_output import FollowUpProposal
from benji_api.config import Settings
from benji_api.models.agent import AgentFollowUp, AgentFollowUpStatus


async def cancel_pending_follow_ups(
    session: AsyncSession,
    *,
    conversation_id: UUID,
) -> int:
    now = datetime.now(UTC)
    result = await session.execute(
        update(AgentFollowUp)
        .where(
            AgentFollowUp.conversation_id == conversation_id,
            AgentFollowUp.status.in_(
                (
                    AgentFollowUpStatus.PENDING.value,
                    AgentFollowUpStatus.PROCESSING.value,
                )
            ),
            AgentFollowUp.cancel_on_user_message.is_(True),
        )
        .values(
            status=AgentFollowUpStatus.CANCELLED.value,
            cancelled_at=now,
            locked_at=None,
        )
    )
    return result.rowcount or 0


async def schedule_follow_up(
    session: AsyncSession,
    *,
    conversation_id: UUID,
    user_id: UUID,
    source_agent_run_id: UUID,
    proposal: FollowUpProposal | None,
    delivery_provider: str | None,
    chain_depth: int,
    settings: Settings,
) -> AgentFollowUp | None:
    if (
        proposal is None
        or not settings.agent_follow_ups_enabled
        or chain_depth >= settings.agent_follow_up_max_chain_depth
    ):
        return None

    existing = await session.scalar(
        select(AgentFollowUp).where(AgentFollowUp.source_agent_run_id == source_agent_run_id)
    )
    if existing is not None:
        return existing

    delay_seconds = max(
        settings.agent_follow_up_min_delay_seconds,
        min(proposal.due_after_seconds, settings.agent_follow_up_max_delay_seconds),
    )
    follow_up = AgentFollowUp(
        conversation_id=conversation_id,
        user_id=user_id,
        source_agent_run_id=source_agent_run_id,
        goal=proposal.goal,
        delivery_provider=delivery_provider,
        chain_depth=chain_depth + 1,
        due_at=datetime.now(UTC) + timedelta(seconds=delay_seconds),
    )
    session.add(follow_up)
    return follow_up
