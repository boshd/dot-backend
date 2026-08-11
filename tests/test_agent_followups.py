from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from benji_api.agents.tools import ToolRegistry
from benji_api.agents.types import (
    AgentMessage,
    ModelSession,
    ModelToolOutput,
    ModelTurn,
    StructuredOutputDefinition,
    ToolDefinition,
)
from benji_api.config import Settings
from benji_api.db.base import Base
from benji_api.models import (
    AgentFollowUp,
    AgentFollowUpStatus,
    AgentRun,
    AgentRunStatus,
    Conversation,
    ConversationChannel,
    Message,
    MessageDirection,
    MessageStatus,
    OnboardingStatus,
    User,
)
from benji_api.services.agent_followups import dispatch_due_follow_up


class FakeLinqClient:
    def __init__(self) -> None:
        self.sent: list[dict[str, str]] = []

    async def send_chat_message(
        self, *, chat_id: str, text: str, idempotency_key: str
    ) -> dict[str, Any]:
        self.sent.append({"chat_id": chat_id, "text": text, "idempotency_key": idempotency_key})
        return {"id": f"follow-up-message-{len(self.sent)}"}

    async def start_typing(self, *, chat_id: str) -> None:
        pass

    async def stop_typing(self, *, chat_id: str) -> None:
        pass


class FakeFollowUpProvider:
    name = "fake"
    model = "fake-follow-up-model"

    def __init__(self) -> None:
        self.started = 0

    def start(
        self,
        *,
        instructions: str,
        messages: list[AgentMessage],
        tools: list[ToolDefinition],
        output: StructuredOutputDefinition | None = None,
    ) -> ModelSession:
        self.started += 1
        assert "scheduled conversational follow-up" in instructions
        assert messages[-1].content == "wanna plan today?"
        assert output is not None and output.name == "benji_conversation_turn"
        return FakeFollowUpSession()

    async def generate_structured(self, **_: object) -> object:
        raise AssertionError("follow-up turns use a regular model session")


class FakeFollowUpSession:
    async def next(self, tool_outputs: tuple[ModelToolOutput, ...] = ()) -> ModelTurn:
        assert tool_outputs == ()
        return ModelTurn(
            response_id="follow-up-response-1",
            text=(
                '{"messages":["want me to pull up the rest of your day?"],'
                '"follow_up":{"should_schedule":false,"goal":"","due_after_seconds":0}}'
            ),
        )


async def _seed_follow_up(
    session_factory: async_sessionmaker,
    *,
    with_newer_user_message: bool,
) -> AgentFollowUp:
    now = datetime.now(UTC)
    async with session_factory() as session:
        user = User(
            phone_number="+14155552671",
            onboarding_status=OnboardingStatus.COMPLETE.value,
        )
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
        trigger = Message(
            conversation_id=conversation.id,
            user_id=user.id,
            source_channel="linq",
            direction=MessageDirection.INBOUND.value,
            status=MessageStatus.RECEIVED.value,
            content="wanna plan today?",
            created_at=now - timedelta(minutes=3),
        )
        session.add(trigger)
        await session.flush()
        source_run = AgentRun(
            conversation_id=conversation.id,
            user_id=user.id,
            trigger_message_id=trigger.id,
            provider="fake",
            model="fake-model",
            status=AgentRunStatus.COMPLETED.value,
            completed_at=now - timedelta(minutes=2),
        )
        session.add(source_run)
        await session.flush()
        follow_up = AgentFollowUp(
            conversation_id=conversation.id,
            user_id=user.id,
            source_agent_run_id=source_run.id,
            goal="offer to review today's remaining schedule",
            delivery_provider="linq",
            due_at=now - timedelta(seconds=1),
            created_at=now - timedelta(minutes=1),
        )
        session.add(follow_up)
        if with_newer_user_message:
            session.add(
                Message(
                    conversation_id=conversation.id,
                    user_id=user.id,
                    source_channel="linq",
                    direction=MessageDirection.INBOUND.value,
                    status=MessageStatus.RECEIVED.value,
                    content="actually never mind",
                    created_at=now,
                )
            )
        await session.commit()
        return follow_up


@pytest.mark.anyio
async def test_due_follow_up_reruns_agent_with_latest_context_and_delivers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    follow_up = await _seed_follow_up(session_factory, with_newer_user_message=False)
    monkeypatch.setattr("benji_api.agents.channel_delivery.async_session_factory", session_factory)
    provider = FakeFollowUpProvider()
    linq = FakeLinqClient()

    handled = await dispatch_due_follow_up(
        settings=Settings(agent_inter_bubble_delay_seconds=0),
        provider=provider,
        tools=ToolRegistry([]),
        linq_client=linq,  # type: ignore[arg-type]
        follow_up_id=follow_up.id,
        session_factory=session_factory,
    )

    assert handled is True
    assert provider.started == 1
    assert [message["text"] for message in linq.sent] == [
        "want me to pull up the rest of your day?"
    ]
    async with session_factory() as session:
        stored = await session.get(AgentFollowUp, follow_up.id)
        assert stored is not None
        assert stored.status == AgentFollowUpStatus.COMPLETED.value
        assert (
            await session.scalar(
                select(func.count())
                .select_from(Message)
                .where(Message.direction == MessageDirection.OUTBOUND.value)
            )
            == 1
        )
    await engine.dispose()


@pytest.mark.anyio
async def test_due_follow_up_is_cancelled_if_user_replied_first() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    follow_up = await _seed_follow_up(session_factory, with_newer_user_message=True)
    provider = FakeFollowUpProvider()
    linq = FakeLinqClient()

    handled = await dispatch_due_follow_up(
        settings=Settings(agent_inter_bubble_delay_seconds=0),
        provider=provider,
        tools=ToolRegistry([]),
        linq_client=linq,  # type: ignore[arg-type]
        follow_up_id=follow_up.id,
        session_factory=session_factory,
    )

    assert handled is True
    assert provider.started == 0
    assert linq.sent == []
    async with session_factory() as session:
        stored = await session.get(AgentFollowUp, follow_up.id)
        assert stored is not None
        assert stored.status == AgentFollowUpStatus.CANCELLED.value
    await engine.dispose()
