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
from benji_api.models.channel import ConversationKind
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


class FakeAppBuildProvider(FakeSilentScheduledProvider):
    def start(
        self,
        *,
        instructions: str,
        messages: list[AgentMessage],
        tools: list[ToolDefinition],
        output: StructuredOutputDefinition | None = None,
    ) -> ModelSession:
        del messages, tools
        assert "app.build.completed" in instructions
        assert "URL must be alone in its own `messages` item" in instructions
        assert output is not None
        return FakeAppBuildSession()


class FakeAppBuildSession:
    async def next(self, tool_outputs: tuple[ModelToolOutput, ...] = ()) -> ModelTurn:
        assert tool_outputs == ()
        return ModelTurn(
            response_id="app-build-response",
            text=(
                '{"messages":["cottage split is ready: https://app.example/a/demo"],'
                '"follow_up":{"should_schedule":false,"goal":"","due_after_seconds":0}}'
            ),
        )


class FakeMangledAppBuildProvider(FakeAppBuildProvider):
    def start(
        self,
        *,
        instructions: str,
        messages: list[AgentMessage],
        tools: list[ToolDefinition],
        output: StructuredOutputDefinition | None = None,
    ) -> ModelSession:
        del messages, tools
        assert "app.build.completed" in instructions
        assert output is not None
        return FakeMangledAppBuildSession()


class FakeMangledAppBuildSession:
    async def next(self, tool_outputs: tuple[ModelToolOutput, ...] = ()) -> ModelTurn:
        assert tool_outputs == ()
        return ModelTurn(
            response_id="app-build-mangled-response",
            text=(
                '{"messages":["cottage split is ready: https://wrong.example/a/nope"],'
                '"follow_up":{"should_schedule":false,"goal":"","due_after_seconds":0}}'
            ),
        )


class FakeFailingAppBuildProvider(FakeAppBuildProvider):
    def start(
        self,
        *,
        instructions: str,
        messages: list[AgentMessage],
        tools: list[ToolDefinition],
        output: StructuredOutputDefinition | None = None,
    ) -> ModelSession:
        del messages, tools
        assert "app.build.completed" in instructions
        assert output is not None
        raise RuntimeError("model provider unavailable")


class FakeFailingBuildFailureProvider(FakeAppBuildProvider):
    def start(
        self,
        *,
        instructions: str,
        messages: list[AgentMessage],
        tools: list[ToolDefinition],
        output: StructuredOutputDefinition | None = None,
    ) -> ModelSession:
        del messages, tools
        assert "app.build.failed" in instructions
        assert output is not None
        raise RuntimeError("model provider unavailable")


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
async def test_app_completion_replaces_model_authored_url_with_exact_trusted_url() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    trusted_url = "https://app.example/a/demo#handoff=trusted-ticket"
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
            event_type="app.build.completed",
            source="generated_app_builder",
            idempotency_key="app-build-completed:trusted-url",
            payload={"title": "Cottage split", "app_url": trusted_url},
        )
        await session.commit()

    handled = await dispatch_user_event(
        settings=Settings(),
        provider=FakeMangledAppBuildProvider(),
        tools=ToolRegistry([]),
        linq_client=None,
        event_id=event.id,
        session_factory=session_factory,
    )

    assert handled is True
    async with session_factory() as session:
        messages = list((await session.scalars(select(Message))).all())
        assert len(messages) == 1
        assert trusted_url in messages[0].content
        assert "wrong.example" not in messages[0].content
    await engine.dispose()


@pytest.mark.anyio
async def test_app_completion_immediately_delivers_canonical_link_when_model_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    trusted_url = "https://app.example/a/demo#handoff=fallback-ticket"
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
        event = await enqueue_user_event(
            session,
            user_id=user.id,
            conversation_id=conversation.id,
            event_type="app.build.completed",
            source="generated_app_builder",
            idempotency_key="app-build-completed:model-exhausted",
            payload={"title": "Cottage split", "app_url": trusted_url},
            delivery_provider="linq",
        )
        await session.commit()

    monkeypatch.setattr("benji_api.agents.channel_delivery.async_session_factory", session_factory)
    fake_linq = FakeLinqClient()
    handled = await dispatch_user_event(
        settings=Settings(user_event_max_attempts=8, agent_inter_bubble_delay_seconds=0),
        provider=FakeFailingAppBuildProvider(),
        tools=ToolRegistry([]),
        linq_client=fake_linq,  # type: ignore[arg-type]
        event_id=event.id,
        session_factory=session_factory,
    )
    handled_again = await dispatch_user_event(
        settings=Settings(user_event_max_attempts=8, agent_inter_bubble_delay_seconds=0),
        provider=FakeFailingAppBuildProvider(),
        tools=ToolRegistry([]),
        linq_client=fake_linq,  # type: ignore[arg-type]
        event_id=event.id,
        session_factory=session_factory,
    )

    assert handled is True
    assert handled_again is False
    assert [sent["text"] for sent in fake_linq.sent] == [trusted_url]
    async with session_factory() as session:
        stored_event = await session.get(UserEvent, event.id)
        message = await session.scalar(select(Message))
        delivery = await session.scalar(select(MessageDelivery))
        run = await session.scalar(select(AgentRun))
        assert stored_event is not None
        assert stored_event.status == UserEventStatus.PROCESSED.value
        assert stored_event.attempts == 1
        assert message is not None and message.content == trusted_url
        assert message.raw_payload["canonical_fallback"] is True
        assert delivery is not None and delivery.status == "sent"
        assert await session.scalar(select(func.count()).select_from(Message)) == 1
        assert await session.scalar(select(func.count()).select_from(MessageDelivery)) == 1
        assert run is not None and run.status == AgentRunStatus.FAILED.value
    await engine.dispose()


@pytest.mark.anyio
async def test_app_failure_persists_plain_fallback_after_model_exhaustion() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with session_factory() as session:
        user = User(phone_number="+14155552673")
        session.add(user)
        await session.flush()
        conversation = Conversation(user_id=user.id)
        session.add(conversation)
        await session.flush()
        event = await enqueue_user_event(
            session,
            user_id=user.id,
            conversation_id=conversation.id,
            event_type="app.build.failed",
            source="generated_app_builder",
            idempotency_key="app-build-failed:model-exhausted",
            payload={"title": "birthday planner", "retryable": False},
        )
        await session.commit()

    handled = await dispatch_user_event(
        settings=Settings(user_event_max_attempts=1),
        provider=FakeFailingBuildFailureProvider(),
        tools=ToolRegistry([]),
        linq_client=None,
        event_id=event.id,
        session_factory=session_factory,
    )

    assert handled is True
    async with session_factory() as session:
        stored_event = await session.get(UserEvent, event.id)
        message = await session.scalar(select(Message))
        assert stored_event is not None
        assert stored_event.status == UserEventStatus.PROCESSED.value
        assert message is not None
        assert "birthday planner" in message.content
        assert "broken link" in message.content
        assert message.raw_payload["canonical_fallback"] is True
    await engine.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("automated_replies_enabled", "client_configured"),
    ((False, True), (True, False)),
)
async def test_unavailable_linq_skips_only_delivery_and_keeps_canonical_turn(
    automated_replies_enabled: bool,
    client_configured: bool,
) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    trusted_url = "https://app.example/a/demo#handoff=offline-ticket"
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
        event = await enqueue_user_event(
            session,
            user_id=user.id,
            conversation_id=conversation.id,
            event_type="app.build.completed",
            source="generated_app_builder",
            idempotency_key=(
                f"app-build-completed:linq-{automated_replies_enabled}-"
                f"{client_configured}"
            ),
            payload={"title": "Cottage split", "app_url": trusted_url},
            delivery_provider="linq",
        )
        await session.commit()

    fake_linq = FakeLinqClient()
    handled = await dispatch_user_event(
        settings=Settings(linq_automated_replies_enabled=automated_replies_enabled),
        provider=FakeAppBuildProvider(),
        tools=ToolRegistry([]),
        linq_client=(fake_linq if client_configured else None),  # type: ignore[arg-type]
        event_id=event.id,
        session_factory=session_factory,
    )

    assert handled is True
    assert fake_linq.sent == []
    async with session_factory() as session:
        stored_event = await session.get(UserEvent, event.id)
        message = await session.scalar(select(Message))
        assert stored_event is not None
        assert stored_event.status == UserEventStatus.PROCESSED.value
        assert message is not None and trusted_url in message.content
        assert await session.scalar(select(func.count()).select_from(MessageDelivery)) == 0
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


@pytest.mark.anyio
async def test_group_event_survives_owner_transfer_while_work_is_in_flight() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with session_factory() as session:
        original_owner = User(phone_number="+14155552671")
        new_owner = User(phone_number="+14155552672")
        session.add_all([original_owner, new_owner])
        await session.flush()
        conversation = Conversation(
            user_id=original_owner.id,
            kind=ConversationKind.GROUP.value,
            title="Cottage",
        )
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
        event = await enqueue_user_event(
            session,
            user_id=original_owner.id,
            conversation_id=conversation.id,
            event_type="app.build.completed",
            source="generated_app_builder",
            idempotency_key="app-build-completed:owner-transfer",
            payload={"title": "Cottage split", "app_url": "https://app.example/a/demo"},
            delivery_provider="linq",
        )
        conversation.user_id = new_owner.id
        original_owner.messaging_opted_out_at = original_owner.created_at
        await session.commit()

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr("benji_api.agents.channel_delivery.async_session_factory", session_factory)
    fake_linq = FakeLinqClient()
    try:
        handled = await dispatch_user_event(
            settings=Settings(agent_inter_bubble_delay_seconds=0),
            provider=FakeAppBuildProvider(),
            tools=ToolRegistry([]),
            linq_client=fake_linq,  # type: ignore[arg-type]
            event_id=event.id,
            session_factory=session_factory,
        )
    finally:
        monkeypatch.undo()

    assert handled is True
    assert len(fake_linq.sent) == 1
    async with session_factory() as session:
        stored_event = await session.get(UserEvent, event.id)
        assert stored_event is not None
        # Reaching the agent proves the durable event was not rejected after the group's
        # owner pointer changed.
        assert stored_event.status == UserEventStatus.PROCESSED.value
    await engine.dispose()
