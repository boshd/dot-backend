from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from benji_api.agents.dependencies import get_model_provider
from benji_api.agents.prompts.base import DOT_PROMPT_VERSION
from benji_api.agents.types import (
    AgentMessage,
    ModelSession,
    ModelToolOutput,
    ModelTurn,
    StructuredModelResult,
    StructuredOutputDefinition,
    ToolDefinition,
)
from benji_api.config import Settings, get_settings
from benji_api.db.base import Base
from benji_api.db.session import get_session
from benji_api.main import app
from benji_api.models import (
    AgentRun,
    AgentRunPurpose,
    Conversation,
    ConversationChannel,
    MemoryEntity,
    MemoryFact,
    MemoryJob,
    Message,
    User,
)


class FakeWebModelProvider:
    name = "fake"
    model = "fake-web-model"
    reasoning_effort = "minimal"

    def __init__(self) -> None:
        self.profile = {
            "display_name": "Kareem",
            "birth_date": "1992-04-18",
            "location_city": "Cairo",
            "location_country": "Egypt",
        }
        self.structured_calls = 0
        self.regular_tool_names: list[str] = []

    def start(
        self,
        *,
        instructions: str,
        messages: list[AgentMessage],
        tools: list[ToolDefinition],
        output: StructuredOutputDefinition | None = None,
    ) -> ModelSession:
        assert 'prompt_module name="onboarding"' not in instructions
        assert 'prompt_module name="personal_memory"' not in instructions
        assert "Kareem likes jazz." not in instructions
        assert messages[-1].content == "what can you do?"
        self.regular_tool_names = [tool.name for tool in tools]
        assert output is not None and output.name == "benji_conversation_turn"
        return FakeWebModelSession()

    async def generate_structured(
        self,
        *,
        instructions: str,
        messages: list[AgentMessage],
        output: StructuredOutputDefinition,
    ) -> StructuredModelResult:
        assert "no capability tools during onboarding" in instructions
        assert messages[-1].content == "i’m Kareem, born April 18 1992 in Cairo"
        assert output.name == "onboarding_turn"
        self.structured_calls += 1
        return StructuredModelResult(
            response_id="web-onboarding-response",
            data={
                "messages": [
                    "got it. what’s one thing you actually want to get done this week?"
                ],
                "profile": self.profile,
            },
            token_usage={"input_tokens": 90, "output_tokens": 18, "total_tokens": 108},
        )


class FakeWebModelSession:
    async def next(self, tool_outputs: tuple[ModelToolOutput, ...] = ()) -> ModelTurn:
        assert tool_outputs == ()
        return ModelTurn(
            response_id="web-conversation-response",
            text=(
                '{"messages":["a lot, eventually.",'
                '"right now i’m very good at talking."],'
                '"follow_up":{"should_schedule":false,"goal":"","due_after_seconds":0}}'
            ),
            token_usage={"input_tokens": 120, "output_tokens": 24, "total_tokens": 144},
        )


@asynccontextmanager
async def web_chat_test_app(
    monkeypatch: pytest.MonkeyPatch,
    *,
    enabled: bool = True,
) -> AsyncIterator[tuple[AsyncClient, async_sessionmaker[AsyncSession], FakeWebModelProvider]]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    model_provider = FakeWebModelProvider()
    settings = Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        web_chat_dev_identity_enabled=enabled,
        memory_enabled=True,
    )

    async def override_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_model_provider] = lambda: model_provider
    monkeypatch.setattr("benji_api.agents.onboarding.async_session_factory", session_factory)
    monkeypatch.setattr("benji_api.agents.service.async_session_factory", session_factory)
    monkeypatch.setattr("benji_api.agents.channel_delivery.async_session_factory", session_factory)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            yield client, session_factory, model_provider
    finally:
        app.dependency_overrides.clear()
        await engine.dispose()


@pytest.mark.anyio
async def test_web_chat_uses_same_onboarding_then_unlocks_regular_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    phone = "+14155552671"
    async with web_chat_test_app(monkeypatch) as (
        client,
        session_factory,
        model_provider,
    ):
        opened = await client.post("/api/v1/web/chat/session", json={"phone_number": phone})
        assert opened.status_code == 200
        conversation_id = opened.json()["conversation_id"]
        assert opened.json()["messages"] == []

        onboarding = await client.post(
            "/api/v1/web/chat/messages",
            json={
                "phone_number": phone,
                "conversation_id": conversation_id,
                "client_message_id": "00000000-0000-0000-0000-000000000001",
                "content": "i’m Kareem, born April 18 1992 in Cairo",
            },
        )
        assert onboarding.status_code == 200
        assert onboarding.json()["user"]["onboarding_status"] == "complete"
        assert model_provider.structured_calls == 1
        assert model_provider.regular_tool_names == []

        async with session_factory() as session:
            user = await session.scalar(select(User))
            assert user is not None
            assert await session.scalar(select(func.count()).select_from(MemoryJob)) == 1
            entity = MemoryEntity(
                user_id=user.id,
                entity_type="person",
                name="user",
                canonical_key="user",
            )
            session.add(entity)
            await session.flush()
            session.add(
                MemoryFact(
                    user_id=user.id,
                    subject_entity_id=entity.id,
                    predicate="likes",
                    object_value="jazz",
                    statement="Kareem likes jazz.",
                    kind="preference",
                    confidence=0.99,
                    importance=4,
                    valid_from=datetime.now(UTC),
                )
            )
            await session.commit()

        regular = await client.post(
            "/api/v1/web/chat/messages",
            json={
                "phone_number": phone,
                "conversation_id": conversation_id,
                "client_message_id": "00000000-0000-0000-0000-000000000002",
                "content": "what can you do?",
            },
        )
        assert regular.status_code == 200
        assert [message["content"] for message in regular.json()["assistant_messages"]] == [
            "a lot, eventually.",
            "right now i’m very good at talking.",
        ]
        assert model_provider.regular_tool_names == [
            "get_current_datetime",
            "list_connected_integrations",
            "schedule_proactive_reachout",
            "list_scheduled_reachouts",
            "cancel_scheduled_reachout",
            "get_financial_overview",
            "search_financial_transactions",
            "create_financial_goal",
            "list_financial_goals",
            "cancel_financial_goal",
            "create_personal_app",
            "list_personal_memories",
            "forget_personal_memories",
        ]

        duplicate = await client.post(
            "/api/v1/web/chat/messages",
            json={
                "phone_number": phone,
                "conversation_id": conversation_id,
                "client_message_id": "00000000-0000-0000-0000-000000000002",
                "content": "what can you do?",
            },
        )
        assert duplicate.status_code == 200
        assert [message["id"] for message in duplicate.json()["assistant_messages"]] == [
            message["id"] for message in regular.json()["assistant_messages"]
        ]

        restored = await client.post(
            "/api/v1/web/chat/session",
            json={"phone_number": phone, "conversation_id": conversation_id},
        )
        assert [message["role"] for message in restored.json()["messages"]] == [
            "user",
            "assistant",
            "user",
            "assistant",
            "assistant",
        ]

        async with session_factory() as session:
            assert await session.scalar(select(func.count()).select_from(User)) == 1
            assert await session.scalar(select(func.count()).select_from(Message)) == 5
            assert await session.scalar(select(func.count()).select_from(Conversation)) == 1
            assert await session.scalar(select(func.count()).select_from(ConversationChannel)) == 1
            assert await session.scalar(select(func.count()).select_from(MemoryJob)) == 2
            runs = (await session.scalars(select(AgentRun).order_by(AgentRun.started_at))).all()
        assert [run.purpose for run in runs] == [
            AgentRunPurpose.ONBOARDING.value,
            AgentRunPurpose.CONVERSATION.value,
        ]
        onboarding_run = runs[0]
        assert onboarding_run.prompt_version == DOT_PROMPT_VERSION
        assert onboarding_run.prompt_hash is not None
        assert len(onboarding_run.prompt_hash) == 64
        assert onboarding_run.prompt_snapshot is not None
        assert onboarding_run.prompt_snapshot["module_names"] == [
            "benji_core",
            "direct_conversation",
            "language_style",
            "user_profile",
            "onboarding",
            "conversation_posture",
        ]
        assert onboarding_run.retrieved_memory == []
        assert onboarding_run.exposed_tools == []
        assert onboarding_run.reasoning_effort == "minimal"
        assert onboarding_run.raw_output == {
            "messages": ["got it. what’s one thing you actually want to get done this week?"],
            "profile": model_provider.profile,
        }
        assert onboarding_run.token_usage == {
            "input_tokens": 90,
            "output_tokens": 18,
            "total_tokens": 108,
        }
        conversation_run = runs[-1]
        assert conversation_run.prompt_version == DOT_PROMPT_VERSION
        assert conversation_run.prompt_hash is not None
        assert len(conversation_run.prompt_hash) == 64
        assert conversation_run.prompt_snapshot is not None
        assert conversation_run.prompt_snapshot["module_names"] == [
            "benji_core",
            "direct_conversation",
            "language_style",
            "user_profile",
            "relationship_state",
            "conversation_posture",
        ]
        assert conversation_run.retrieved_memory == []
        assert conversation_run.exposed_tools == model_provider.regular_tool_names
        assert conversation_run.reasoning_effort == "minimal"
        assert conversation_run.raw_output == {
            "messages": ["a lot, eventually.", "right now i’m very good at talking."],
            "follow_up": {
                "should_schedule": False,
                "goal": "",
                "due_after_seconds": 0,
            },
        }
        assert conversation_run.token_usage == {
            "input_tokens": 120,
            "output_tokens": 24,
            "total_tokens": 144,
        }


@pytest.mark.anyio
async def test_web_phone_identity_endpoint_can_be_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with web_chat_test_app(monkeypatch, enabled=False) as (client, _, _):
        response = await client.post(
            "/api/v1/web/chat/session",
            json={"phone_number": "+14155552671"},
        )

    assert response.status_code == 401
