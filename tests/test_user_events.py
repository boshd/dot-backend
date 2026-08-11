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
    MessageDelivery,
    User,
    UserEvent,
    UserEventStatus,
)
from benji_api.services.user_events import dispatch_user_event, enqueue_user_event


class FakeLinqClient:
    def __init__(self) -> None:
        self.sent: list[dict[str, str]] = []
        self.typing: list[bool] = []

    async def send_chat_message(
        self, *, chat_id: str, text: str, idempotency_key: str
    ) -> dict[str, Any]:
        self.sent.append({"chat_id": chat_id, "text": text, "idempotency_key": idempotency_key})
        return {"id": f"event-message-{len(self.sent)}"}

    async def start_typing(self, *, chat_id: str) -> None:
        assert chat_id == "chat-1"
        self.typing.append(True)

    async def stop_typing(self, *, chat_id: str) -> None:
        assert chat_id == "chat-1"
        self.typing.append(False)


class FakeEventProvider:
    name = "fake"
    model = "fake-event-model"

    def start(
        self,
        *,
        instructions: str,
        messages: list[AgentMessage],
        tools: list[ToolDefinition],
        output: StructuredOutputDefinition | None = None,
    ) -> ModelSession:
        assert "integration.connected" in instructions
        assert output is not None and output.name == "benji_conversation_turn"
        return FakeEventSession()

    async def generate_structured(self, **_: object) -> object:
        raise AssertionError("event turns do not use the standalone structured path")


class FakeEventSession:
    async def next(self, tool_outputs: tuple[ModelToolOutput, ...] = ()) -> ModelTurn:
        assert tool_outputs == ()
        return ModelTurn(
            response_id="event-response-1",
            text=(
                '{"messages":["sweeet, i can see your calendar now",'
                '"wanna see what the rest of today looks like?"],'
                '"follow_up":{"should_schedule":true,'
                '"goal":"offer a quick calendar overview if they stay silent",'
                '"due_after_seconds":120}}'
            ),
        )


class FakeSilentScheduledProvider:
    name = "fake"
    model = "fake-scheduled-model"

    def start(
        self,
        *,
        instructions: str,
        messages: list[AgentMessage],
        tools: list[ToolDefinition],
        output: StructuredOutputDefinition | None = None,
    ) -> ModelSession:
        del messages, tools
        assert "schedule.triggered" in instructions
        assert output is not None
        return FakeSilentScheduledSession()

    async def generate_structured(self, **_: object) -> object:
        raise AssertionError("scheduled events use a regular model session")


class FakeSilentScheduledSession:
    async def next(self, tool_outputs: tuple[ModelToolOutput, ...] = ()) -> ModelTurn:
        assert tool_outputs == ()
        return ModelTurn(
            response_id="scheduled-silent-response",
            text=(
                '{"messages":[],"follow_up":{"should_schedule":false,'
                '"goal":"","due_after_seconds":0}}'
            ),
        )


@pytest.mark.anyio
async def test_connected_integration_event_double_texts_once_and_persists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
        channel = ConversationChannel(
            conversation_id=conversation.id,
            provider="linq",
            external_id="chat-1",
            status="active",
        )
        session.add(channel)
        event = await enqueue_user_event(
            session,
            user_id=user.id,
            event_type="integration.connected",
            source="integration_oauth",
            idempotency_key="integration.connected:test",
            payload={
                "integration_key": "google_calendar",
                "account_email": "kareem@example.com",
            },
            delivery_provider="linq",
        )
        await session.commit()

    monkeypatch.setattr("benji_api.agents.channel_delivery.async_session_factory", session_factory)
    fake_linq = FakeLinqClient()
    settings = Settings(linq_api_key="test-key", agent_inter_bubble_delay_seconds=0)
    provider = FakeEventProvider()
    tools = ToolRegistry([])
    handled = await dispatch_user_event(
        settings=settings,
        provider=provider,
        tools=tools,
        linq_client=fake_linq,  # type: ignore[arg-type]
        event_id=event.id,
        session_factory=session_factory,
    )
    handled_again = await dispatch_user_event(
        settings=settings,
        provider=provider,
        tools=tools,
        linq_client=fake_linq,  # type: ignore[arg-type]
        event_id=event.id,
        session_factory=session_factory,
    )

    assert handled is True
    assert handled_again is False
    assert len(fake_linq.sent) == 2
    assert fake_linq.sent[0]["chat_id"] == "chat-1"
    assert "i can see your calendar now" in fake_linq.sent[0]["text"]
    assert "rest of today" in fake_linq.sent[1]["text"]
    assert fake_linq.typing == [True, True, False]

    async with session_factory() as session:
        stored_event = await session.get(UserEvent, event.id)
        message = await session.scalar(select(Message))
        delivery = await session.scalar(select(MessageDelivery))
        follow_up = await session.scalar(select(AgentFollowUp))
        assert stored_event is not None
        assert stored_event.status == UserEventStatus.PROCESSED.value
        assert stored_event.attempts == 1
        assert message is not None and message.source_channel == "event"
        assert delivery is not None and delivery.status == "sent"
        assert follow_up is not None
        assert follow_up.status == AgentFollowUpStatus.PENDING.value
        assert "calendar overview" in follow_up.goal
        assert await session.scalar(select(func.count()).select_from(Message)) == 2

    await engine.dispose()


@pytest.mark.anyio
async def test_scheduled_event_can_complete_silently_when_nothing_is_useful() -> None:
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
        event = await enqueue_user_event(
            session,
            user_id=user.id,
            conversation_id=conversation.id,
            event_type="schedule.triggered",
            source="test",
            idempotency_key="schedule.triggered:silent-test",
            payload={"goal": "only say something if spending changed meaningfully"},
        )
        await session.commit()

    handled = await dispatch_user_event(
        settings=Settings(),
        provider=FakeSilentScheduledProvider(),
        tools=ToolRegistry([]),
        linq_client=None,
        event_id=event.id,
        session_factory=session_factory,
    )

    assert handled is True
    async with session_factory() as session:
        stored_event = await session.get(UserEvent, event.id)
        run = await session.scalar(select(AgentRun))
        assert stored_event is not None
        assert stored_event.status == UserEventStatus.PROCESSED.value
        assert run is not None and run.status == AgentRunStatus.COMPLETED.value
        assert await session.scalar(select(func.count()).select_from(Message)) == 0
    await engine.dispose()
