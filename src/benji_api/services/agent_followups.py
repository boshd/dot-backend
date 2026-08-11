import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from benji_api.agents.channel_delivery import deliver_linq_replies, set_typing
from benji_api.agents.service import AgentWakeCancelled, run_follow_up_turn
from benji_api.agents.tools import ToolRegistry
from benji_api.agents.types import ModelProvider
from benji_api.config import Settings
from benji_api.db.session import async_session_factory
from benji_api.integrations.linq.client import LinqClient
from benji_api.memory.types import EmbeddingProvider
from benji_api.models.agent import AgentFollowUp, AgentFollowUpStatus
from benji_api.models.channel import ConversationChannel, Message, MessageDirection
from benji_api.models.user import User

logger = logging.getLogger(__name__)


async def dispatch_due_follow_up(
    *,
    settings: Settings,
    provider: ModelProvider | None,
    tools: ToolRegistry,
    linq_client: LinqClient | None,
    embedding_provider: EmbeddingProvider | None = None,
    follow_up_id: UUID | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> bool:
    factory = session_factory or async_session_factory
    follow_up = await _claim_follow_up(
        factory,
        max_attempts=settings.agent_follow_up_max_attempts,
        follow_up_id=follow_up_id,
    )
    if follow_up is None:
        return False

    chat_id: str | None = None
    typing_started = False
    try:
        if await _should_cancel(factory, follow_up):
            await _cancel_follow_up(
                factory,
                follow_up.id,
                "Cancelled because the user replied or messaging is unavailable",
            )
            return True
        if provider is None:
            raise RuntimeError("The follow-up agent model provider is not configured")

        channel = await _resolve_channel(factory, follow_up)
        if follow_up.delivery_provider is not None and channel is None:
            await _cancel_follow_up(
                factory,
                follow_up.id,
                f"No active {follow_up.delivery_provider} channel",
            )
            return True
        if follow_up.delivery_provider == "linq":
            if not settings.linq_automated_replies_enabled:
                await _cancel_follow_up(
                    factory, follow_up.id, "Automated Linq delivery is disabled"
                )
                return True
            if linq_client is None or channel is None:
                raise RuntimeError("Linq delivery is not configured")
            chat_id = channel.external_id
            await set_typing(linq_client, chat_id=chat_id, active=True)
            typing_started = True
        elif follow_up.delivery_provider is not None:
            raise RuntimeError(f"No delivery adapter for {follow_up.delivery_provider}")

        turn = await run_follow_up_turn(
            conversation_id=follow_up.conversation_id,
            user_id=follow_up.user_id,
            follow_up_id=follow_up.id,
            goal=follow_up.goal,
            source_binding_id=channel.id if channel is not None else None,
            delivery_provider=follow_up.delivery_provider,
            provider=provider,
            tools=tools,
            settings=settings,
            embedding_provider=embedding_provider,
            session_factory=factory,
        )
        if follow_up.delivery_provider == "linq" and channel is not None and chat_id is not None:
            await deliver_linq_replies(
                replies=turn.replies,
                channel_id=channel.id,
                chat_id=chat_id,
                idempotency_key=f"benji:follow-up:{follow_up.id}",
                client=linq_client,
                inter_message_delay_seconds=settings.agent_inter_bubble_delay_seconds,
                typing_between_messages=True,
                typing_seconds_per_character=settings.agent_typing_seconds_per_character,
                typing_max_delay_seconds=settings.agent_typing_max_delay_seconds,
            )
        await _complete_follow_up(factory, follow_up.id)
    except AgentWakeCancelled:
        await _cancel_follow_up(factory, follow_up.id, "Cancelled by a new user message")
    except Exception as error:
        logger.exception("Agent follow-up %s failed", follow_up.id)
        await _retry_follow_up(factory, follow_up.id, error)
    finally:
        if typing_started and linq_client is not None and chat_id is not None:
            await set_typing(linq_client, chat_id=chat_id, active=False)
    return True


async def _claim_follow_up(
    factory: async_sessionmaker[AsyncSession],
    *,
    max_attempts: int,
    follow_up_id: UUID | None,
) -> AgentFollowUp | None:
    now = datetime.now(UTC)
    stale_before = now - timedelta(minutes=2)
    eligible = or_(
        and_(
            AgentFollowUp.status == AgentFollowUpStatus.PENDING.value,
            AgentFollowUp.due_at <= now,
        ),
        and_(
            AgentFollowUp.status == AgentFollowUpStatus.FAILED.value,
            AgentFollowUp.due_at <= now,
        ),
        and_(
            AgentFollowUp.status == AgentFollowUpStatus.PROCESSING.value,
            AgentFollowUp.locked_at <= stale_before,
        ),
    )
    async with factory() as session:
        statement = (
            select(AgentFollowUp)
            .where(eligible, AgentFollowUp.attempts < max_attempts)
            .order_by(AgentFollowUp.due_at, AgentFollowUp.created_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if follow_up_id is not None:
            statement = statement.where(AgentFollowUp.id == follow_up_id)
        follow_up = await session.scalar(statement)
        if follow_up is None:
            return None
        follow_up.status = AgentFollowUpStatus.PROCESSING.value
        follow_up.attempts += 1
        follow_up.locked_at = now
        follow_up.error = None
        await session.commit()
        return follow_up


async def _should_cancel(
    factory: async_sessionmaker[AsyncSession],
    follow_up: AgentFollowUp,
) -> bool:
    async with factory() as session:
        user = await session.get(User, follow_up.user_id)
        if user is None or user.messaging_opted_out_at is not None:
            return True
        if not follow_up.cancel_on_user_message:
            return False
        newer_inbound = await session.scalar(
            select(Message.id)
            .where(
                Message.conversation_id == follow_up.conversation_id,
                Message.direction == MessageDirection.INBOUND.value,
                Message.created_at > follow_up.created_at,
            )
            .limit(1)
        )
        return newer_inbound is not None


async def _resolve_channel(
    factory: async_sessionmaker[AsyncSession],
    follow_up: AgentFollowUp,
) -> ConversationChannel | None:
    if follow_up.delivery_provider is None:
        return None
    async with factory() as session:
        return await session.scalar(
            select(ConversationChannel)
            .where(
                ConversationChannel.conversation_id == follow_up.conversation_id,
                ConversationChannel.provider == follow_up.delivery_provider,
                ConversationChannel.status == "active",
            )
            .order_by(ConversationChannel.updated_at.desc())
            .limit(1)
        )


async def _complete_follow_up(
    factory: async_sessionmaker[AsyncSession], follow_up_id: UUID
) -> None:
    async with factory() as session:
        follow_up = await session.get(AgentFollowUp, follow_up_id)
        if follow_up is None or follow_up.status == AgentFollowUpStatus.CANCELLED.value:
            return
        follow_up.status = AgentFollowUpStatus.COMPLETED.value
        follow_up.completed_at = datetime.now(UTC)
        follow_up.locked_at = None
        await session.commit()


async def _cancel_follow_up(
    factory: async_sessionmaker[AsyncSession], follow_up_id: UUID, reason: str
) -> None:
    async with factory() as session:
        follow_up = await session.get(AgentFollowUp, follow_up_id)
        if follow_up is None or follow_up.status == AgentFollowUpStatus.COMPLETED.value:
            return
        follow_up.status = AgentFollowUpStatus.CANCELLED.value
        follow_up.cancelled_at = datetime.now(UTC)
        follow_up.locked_at = None
        follow_up.error = reason[:2_000]
        await session.commit()


async def _retry_follow_up(
    factory: async_sessionmaker[AsyncSession], follow_up_id: UUID, error: Exception
) -> None:
    async with factory() as session:
        follow_up = await session.get(AgentFollowUp, follow_up_id)
        if follow_up is None or follow_up.status == AgentFollowUpStatus.CANCELLED.value:
            return
        delay_seconds = min(2 ** max(follow_up.attempts, 1), 300)
        follow_up.status = AgentFollowUpStatus.FAILED.value
        follow_up.error = str(error)[:2_000]
        follow_up.locked_at = None
        follow_up.due_at = datetime.now(UTC) + timedelta(seconds=delay_seconds)
        await session.commit()
