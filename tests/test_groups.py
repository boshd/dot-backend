from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlparse
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from benji_api.agents.dependencies import get_model_provider
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
    Conversation,
    ConversationChannel,
    ConversationMember,
    Message,
    User,
)
from benji_api.services.groups import apply_linq_group_event


class GroupModelProvider:
    name = "fake"
    model = "fake-group-model"

    def __init__(self) -> None:
        self.calls = 0
        self.tool_names: list[str] = []

    def start(
        self,
        *,
        instructions: str,
        messages: list[AgentMessage],
        tools: list[ToolDefinition],
        output: StructuredOutputDefinition | None = None,
    ) -> ModelSession:
        assert 'prompt_module name="group_conversation"' in instructions
        assert 'prompt_module name="user_profile"' not in instructions
        assert 'prompt_module name="conversation_posture"' in instructions
        assert "never reveal" in instructions
        assert messages[-1].content == "[Alice]: hey dot, help us plan"
        assert output is not None and output.name == "benji_conversation_turn"
        self.calls += 1
        self.tool_names = [tool.name for tool in tools]
        return GroupModelSession()

    async def generate_structured(
        self,
        *,
        instructions: str,
        messages: list[AgentMessage],
        output: StructuredOutputDefinition,
    ) -> StructuredModelResult:
        assert output.name == "dot_group_participation"
        assert "ordinary-friend threshold" in instructions
        return StructuredModelResult(
            response_id="group-participation-1",
            data={
                "should_respond": False,
                "send_acknowledgment": False,
                "acknowledgment": "",
            },
        )


class GroupModelSession:
    async def next(self, tool_outputs: tuple[ModelToolOutput, ...] = ()) -> ModelTurn:
        assert tool_outputs == ()
        return ModelTurn(
            response_id="group-response-1",
            text=(
                '{"messages":["yep — what are we planning?"],'
                '"follow_up":{"should_schedule":false,"goal":"",'
                '"due_after_seconds":0}}'
            ),
        )


@asynccontextmanager
async def group_test_app(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[AsyncClient, async_sessionmaker[AsyncSession], GroupModelProvider]]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    provider = GroupModelProvider()
    settings = Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        web_chat_dev_identity_enabled=True,
        memory_enabled=True,
        web_app_url="http://localhost:3000",
    )

    async def override_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_model_provider] = lambda: provider
    monkeypatch.setattr("benji_api.agents.service.async_session_factory", session_factory)
    monkeypatch.setattr("benji_api.agents.channel_delivery.async_session_factory", session_factory)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield client, session_factory, provider
    finally:
        app.dependency_overrides.clear()
        await engine.dispose()


@pytest.mark.anyio
async def test_web_group_create_invite_join_and_shared_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alice_phone = "+14155552671"
    bob_phone = "+14155552672"
    eve_phone = "+14155552673"
    async with group_test_app(monkeypatch) as (client, session_factory, provider):
        await client.post("/api/v1/web/chat/session", json={"phone_number": alice_phone})
        async with session_factory() as session:
            alice = await session.scalar(select(User).where(User.phone_number == alice_phone))
            assert alice is not None
            alice.display_name = "Alice"
            alice.onboarding_status = "complete"
            await session.commit()

        created = await client.post(
            "/api/v1/web/conversations/groups",
            json={"phone_number": alice_phone, "title": "Safari crew"},
        )
        assert created.status_code == 200
        group_id = created.json()["id"]
        assert created.json()["kind"] == "group"
        assert created.json()["members"][0]["display_name"] == "Alice"

        invite = await client.post(
            f"/api/v1/web/conversations/groups/{group_id}/invites",
            json={"phone_number": alice_phone},
        )
        token = parse_qs(urlparse(invite.json()["invite_url"]).query)["group_invite"][0]
        joined = await client.post(
            "/api/v1/web/conversations/groups/join",
            json={"phone_number": bob_phone, "token": token},
        )
        assert joined.status_code == 200
        assert len(joined.json()["members"]) == 2

        opened = await client.post(
            "/api/v1/web/chat/session",
            json={"phone_number": bob_phone, "conversation_id": group_id},
        )
        assert opened.status_code == 200
        assert opened.json()["conversation_kind"] == "group"

        first_turn = await client.post(
            "/api/v1/web/chat/messages",
            json={
                "phone_number": alice_phone,
                "conversation_id": group_id,
                "client_message_id": "00000000-0000-0000-0000-000000000101",
                "content": "hey dot, help us plan",
            },
        )
        assert first_turn.status_code == 200
        assert first_turn.json()["replied"] is True
        assert first_turn.json()["assistant_message"]["content"] == ("yep, what are we planning?")
        assert provider.calls == 1
        assert provider.tool_names == ["get_current_datetime", "create_personal_app"]

        quiet_turn = await client.post(
            "/api/v1/web/chat/messages",
            json={
                "phone_number": bob_phone,
                "conversation_id": group_id,
                "client_message_id": "00000000-0000-0000-0000-000000000102",
                "content": "alice, saturday works for me",
            },
        )
        assert quiet_turn.status_code == 200
        assert quiet_turn.json()["replied"] is False
        assert provider.calls == 1

        shared = await client.post(
            "/api/v1/web/chat/session",
            json={"phone_number": bob_phone, "conversation_id": group_id},
        )
        alice_message = shared.json()["messages"][0]
        assert alice_message["sender_display_name"] == "Alice"
        assert alice_message["is_current_user"] is False

        forbidden = await client.post(
            "/api/v1/web/chat/session",
            json={"phone_number": eve_phone, "conversation_id": group_id},
        )
        assert forbidden.status_code == 404

        async with session_factory() as session:
            assert (
                await session.scalar(select(func.count()).select_from(ConversationMember)) == 3
            )  # Alice's direct membership plus Alice and Bob in the group.
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(Message)
                    .where(Message.conversation_id == UUID(group_id))
                )
                == 3
            )
            group = await session.get(Conversation, UUID(group_id))
            assert group is not None and group.kind == "group"


@pytest.mark.anyio
async def test_linq_group_owner_transfers_when_owner_leaves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with group_test_app(monkeypatch) as (_, session_factory, _):
        async with session_factory() as session:
            owner = User(phone_number="+14155552671", display_name="Alice")
            successor = User(phone_number="+14155552672", display_name="Bob")
            session.add_all([owner, successor])
            await session.flush()
            group = Conversation(
                user_id=owner.id,
                kind="group",
                title="Trip",
                group_owner_source="first_invoker",
            )
            session.add(group)
            await session.flush()
            session.add_all(
                [
                    ConversationChannel(
                        conversation_id=group.id,
                        provider="linq",
                        external_id="linq-transfer-chat",
                    ),
                    ConversationMember(
                        conversation_id=group.id,
                        user_id=owner.id,
                        external_handle=owner.phone_number,
                        role="owner",
                    ),
                    ConversationMember(
                        conversation_id=group.id,
                        user_id=successor.id,
                        external_handle=successor.phone_number,
                    ),
                ]
            )
            await session.commit()

            handled = await apply_linq_group_event(
                session,
                event_type="participant.removed",
                data={
                    "chat_id": "linq-transfer-chat",
                    "participant": {
                        "handle": owner.phone_number,
                        "status": "removed",
                    },
                },
            )
            await session.commit()
            members = (
                await session.scalars(
                    select(ConversationMember).where(ConversationMember.conversation_id == group.id)
                )
            ).all()

        assert handled is True
        assert group.user_id == successor.id
        assert group.group_owner_source == "transferred"
        assert next(member for member in members if member.user_id == successor.id).role == (
            "owner"
        )
