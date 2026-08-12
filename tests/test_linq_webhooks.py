import base64
import hashlib
import hmac
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

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
from benji_api.integrations.linq.dependencies import get_linq_client
from benji_api.integrations.linq.schemas import LinqInboundMessage, LinqWebhookEnvelope
from benji_api.main import app
from benji_api.models.agent import AgentRun, AgentRunPurpose
from benji_api.models.channel import (
    Conversation,
    ConversationChannel,
    ConversationMember,
    Message,
    MessageAttachment,
    MessageDelivery,
    WebhookEvent,
)
from benji_api.models.user import (
    OnboardingStatus,
    OnboardingStep,
    User,
    UserIdentifier,
    UserIdentifierKind,
)
from benji_api.models.user_event import UserEvent, UserEventStatus

RAW_SECRET = b"benji-test-webhook-secret-32byte"
WEBHOOK_SECRET = f"whsec_{base64.b64encode(RAW_SECRET).decode()}"


class FakeLinqClient:
    def __init__(self) -> None:
        self.sent: list[dict[str, str]] = []
        self.typing: list[bool] = []
        self.read_chats: list[str] = []
        self.chats: dict[str, dict[str, Any]] = {}

    async def send_chat_message(
        self, *, chat_id: str, text: str, idempotency_key: str
    ) -> dict[str, Any]:
        self.sent.append({"chat_id": chat_id, "text": text, "idempotency_key": idempotency_key})
        external_id = (
            "outbound-message-id"
            if len(self.sent) == 1
            else f"outbound-message-id-{len(self.sent)}"
        )
        return {"id": external_id}

    async def start_typing(self, *, chat_id: str) -> None:
        assert chat_id == "chat-1"
        self.typing.append(True)

    async def stop_typing(self, *, chat_id: str) -> None:
        assert chat_id == "chat-1"
        self.typing.append(False)

    async def mark_chat_read(self, *, chat_id: str) -> None:
        self.read_chats.append(chat_id)

    async def get_chat(self, *, chat_id: str) -> dict[str, Any]:
        return self.chats.get(chat_id, {})


class FakeModelProvider:
    name = "fake"
    model = "fake-onboarding-model"
    reasoning_effort = "minimal"

    def __init__(self) -> None:
        self.structured_calls: list[StructuredOutputDefinition] = []
        self.profile = {
            "display_name": None,
            "birth_date": None,
            "location_city": None,
            "location_country": None,
        }
        self.regular_tool_names: list[str] = []
        self.response_text = "all set — what’s up?"
        self.group_should_respond = False
        self.group_acknowledgment = ""
        self.last_regular_messages: list[AgentMessage] = []
        self.structured_messages: list[list[AgentMessage]] = []

    def start(
        self,
        *,
        instructions: str,
        messages: list[AgentMessage],
        tools: list[ToolDefinition],
        output: StructuredOutputDefinition | None = None,
    ) -> ModelSession:
        assert 'prompt_module name="onboarding"' not in instructions
        assert messages[-1].content
        self.last_regular_messages = messages
        self.regular_tool_names = [tool.name for tool in tools]
        assert output is not None and output.name == "benji_conversation_turn"
        return FakeModelSession(self.response_text)

    async def generate_structured(
        self,
        *,
        instructions: str,
        messages: list[AgentMessage],
        output: StructuredOutputDefinition,
    ) -> StructuredModelResult:
        self.structured_messages.append(messages)
        if output.name == "dot_group_participation":
            return StructuredModelResult(
                response_id="group-participation-1",
                data={
                    "should_respond": self.group_should_respond,
                    "send_acknowledgment": bool(self.group_acknowledgment),
                    "acknowledgment": self.group_acknowledgment,
                },
            )
        assert "no capability tools during onboarding" in instructions
        assert messages[-1].content
        self.structured_calls.append(output)
        return StructuredModelResult(
            response_id="onboarding-response-1",
            data={
                "messages": ["hey, i’m dot — save me to your contacts. what should i call you?"],
                "profile": self.profile,
            },
            token_usage={"input_tokens": 80, "output_tokens": 16, "total_tokens": 96},
        )


class FakeModelSession:
    def __init__(self, response_text: str) -> None:
        self._response_text = response_text

    async def next(self, tool_outputs: tuple[ModelToolOutput, ...] = ()) -> ModelTurn:
        assert tool_outputs == ()
        return ModelTurn(response_id="conversation-response-1", text=self._response_text)


@asynccontextmanager
async def linq_test_app(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[
    tuple[
        AsyncClient,
        async_sessionmaker[AsyncSession],
        FakeLinqClient,
        FakeModelProvider,
    ]
]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    fake_linq = FakeLinqClient()
    fake_model = FakeModelProvider()
    settings = Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        linq_api_key="test-api-key",
        linq_webhook_secret=WEBHOOK_SECRET,
        agent_group_ack_settle_seconds=0,
    )

    async def override_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_linq_client] = lambda: fake_linq
    app.dependency_overrides[get_model_provider] = lambda: fake_model
    monkeypatch.setattr("benji_api.agents.onboarding.async_session_factory", session_factory)
    monkeypatch.setattr("benji_api.agents.channel_delivery.async_session_factory", session_factory)
    monkeypatch.setattr("benji_api.agents.service.async_session_factory", session_factory)
    monkeypatch.setattr("benji_api.agents.group_turn.async_session_factory", session_factory)
    monkeypatch.setattr("benji_api.services.user_events.async_session_factory", session_factory)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield client, session_factory, fake_linq, fake_model
    finally:
        app.dependency_overrides.clear()
        await engine.dispose()


def message_received_payload(
    *,
    event_id: str = "event-1",
    message_id: str = "message-1",
    text: str = "Hello",
    chat_id: str = "chat-1",
    sender_phone: str = "+14155552671",
    is_group: bool = False,
    service: str = "iMessage",
    parts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "api_version": "v3",
        "webhook_version": "2026-02-03",
        "event_type": "message.received",
        "event_id": event_id,
        "created_at": "2026-08-09T12:00:00Z",
        "trace_id": "trace-1",
        "partner_id": "partner-1",
        "data": {
            "chat": {
                "id": chat_id,
                "is_group": is_group,
                "owner_handle": {"handle": "+16463038325", "is_me": True},
            },
            "id": message_id,
            "direction": "inbound",
            "sender_handle": {
                "handle": sender_phone,
                "is_me": False,
                "service": service,
            },
            "parts": parts if parts is not None else [{"type": "text", "value": text}],
            "service": service,
        },
    }


def signed_headers(body: bytes, *, valid: bool = True) -> dict[str, str]:
    timestamp = str(int(time.time()))
    webhook_id = "delivery-1"
    signature = base64.b64encode(
        hmac.new(
            RAW_SECRET,
            b".".join((webhook_id.encode(), timestamp.encode(), body)),
            hashlib.sha256,
        ).digest()
    ).decode()
    if not valid:
        signature = base64.b64encode(b"invalid-signature").decode()
    return {
        "content-type": "application/json",
        "webhook-id": webhook_id,
        "webhook-timestamp": timestamp,
        "webhook-signature": f"v1,{signature}",
    }


@pytest.mark.anyio
async def test_first_linq_message_creates_user_and_sends_onboarding_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = message_received_payload()
    body = json.dumps(payload, separators=(",", ":")).encode()

    async with linq_test_app(monkeypatch) as (
        client,
        session_factory,
        fake_linq,
        fake_model,
    ):
        response = await client.post(
            "/api/v1/webhooks/linq?version=2026-02-03",
            content=body,
            headers=signed_headers(body),
        )

        assert response.status_code == 200
        assert response.json()["reply_scheduled"] is True
        assert fake_linq.sent[0]["chat_id"] == "chat-1"
        assert "what should i call you?" in fake_linq.sent[0]["text"]

        web_session = await client.post(
            "/api/v1/web/chat/session",
            json={"phone_number": "+14155552671"},
        )
        assert web_session.status_code == 200
        assert [message["role"] for message in web_session.json()["messages"]] == [
            "user",
            "assistant",
        ]

        async with session_factory() as session:
            user = await session.scalar(select(User))
            identifier = await session.scalar(select(UserIdentifier))
            conversation = await session.scalar(select(Conversation))
            member = await session.scalar(select(ConversationMember))
            inbound = await session.scalar(select(Message).where(Message.direction == "inbound"))
            outbound = await session.scalar(select(Message).where(Message.direction == "outbound"))
            conversation_count = await session.scalar(
                select(func.count()).select_from(Conversation)
            )
            channel_count = await session.scalar(
                select(func.count()).select_from(ConversationChannel)
            )
            delivery = await session.scalar(select(MessageDelivery))
            run = await session.scalar(select(AgentRun))

        assert user is not None
        assert user.phone_number == "+14155552671"
        assert user.onboarding_status == OnboardingStatus.COLLECTING_PROFILE.value
        assert user.onboarding_step == OnboardingStep.NAME.value
        assert identifier is not None
        assert identifier.user_id == user.id
        assert identifier.kind == UserIdentifierKind.PHONE.value
        assert identifier.normalized_value == "+14155552671"
        assert identifier.source == "linq"
        assert identifier.verified_at is not None
        assert identifier.is_primary is True
        assert conversation is not None
        assert conversation.user_id == user.id
        assert conversation.kind == "direct"
        assert member is not None
        assert member.conversation_id == conversation.id
        assert member.user_id == user.id
        assert member.external_handle == "+14155552671"
        assert inbound is not None and inbound.content == "Hello"
        assert inbound.conversation_id == conversation.id
        assert inbound.user_id == user.id
        assert outbound is not None and outbound.status == "completed"
        assert outbound.conversation_id == conversation.id
        assert outbound.user_id == user.id
        assert conversation_count == 1
        assert channel_count == 2
        assert web_session.json()["conversation_id"] == str(inbound.conversation_id)
        assert delivery is not None and delivery.status == "sent"
        assert delivery.external_id == "outbound-message-id"
        assert run is not None and run.purpose == AgentRunPurpose.ONBOARDING.value
        assert run.prompt_version == DOT_PROMPT_VERSION
        assert run.prompt_hash is not None and len(run.prompt_hash) == 64
        assert run.prompt_snapshot is not None
        assert run.prompt_snapshot["module_names"] == [
            "benji_core",
            "direct_conversation",
            "language_style",
            "user_profile",
            "onboarding",
            "conversation_posture",
        ]
        assert run.retrieved_memory == []
        assert run.exposed_tools == []
        assert run.reasoning_effort == "minimal"
        assert run.raw_output == {
            "messages": ["hey, i’m dot — save me to your contacts. what should i call you?"],
            "profile": fake_model.profile,
        }
        assert run.token_usage == {
            "input_tokens": 80,
            "output_tokens": 16,
            "total_tokens": 96,
        }
        assert fake_model.structured_calls[0].name == "onboarding_turn"
        assert fake_linq.read_chats == ["chat-1"]
        assert fake_linq.typing == [True, False]


@pytest.mark.anyio
async def test_duplicate_linq_event_is_acknowledged_without_a_second_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = json.dumps(message_received_payload(), separators=(",", ":")).encode()

    async with linq_test_app(monkeypatch) as (
        client,
        session_factory,
        fake_linq,
        _,
    ):
        first = await client.post(
            "/api/v1/webhooks/linq", content=body, headers=signed_headers(body)
        )
        second = await client.post(
            "/api/v1/webhooks/linq", content=body, headers=signed_headers(body)
        )

        assert first.status_code == 200
        assert second.status_code == 200
        assert second.json()["duplicate"] is True
        assert len(fake_linq.sent) == 1
        async with session_factory() as session:
            event_count = await session.scalar(select(func.count()).select_from(WebhookEvent))
        assert event_count == 1


@pytest.mark.anyio
async def test_linq_read_receipts_are_skipped_for_sms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = message_received_payload(service="SMS")
    body = json.dumps(payload, separators=(",", ":")).encode()

    async with linq_test_app(monkeypatch) as (client, _, fake_linq, _):
        response = await client.post(
            "/api/v1/webhooks/linq",
            content=body,
            headers=signed_headers(body),
        )

        assert response.status_code == 200
        assert fake_linq.read_chats == []


@pytest.mark.anyio
async def test_first_email_linq_message_creates_user_and_sends_onboarding_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = message_received_payload(sender_phone="person@example.com")
    body = json.dumps(payload, separators=(",", ":")).encode()

    async with linq_test_app(monkeypatch) as (
        client,
        session_factory,
        fake_linq,
        fake_model,
    ):
        response = await client.post(
            "/api/v1/webhooks/linq",
            content=body,
            headers=signed_headers(body),
        )

        assert response.status_code == 200
        assert response.json()["reply_scheduled"] is True
        assert fake_linq.sent == [
            {
                "chat_id": "chat-1",
                "text": "hey, i’m dot, save me to your contacts. what should i call you?",
                "idempotency_key": "benji:event-1:onboarding",
            }
        ]
        async with session_factory() as session:
            event = await session.scalar(select(WebhookEvent))
            user = await session.scalar(select(User))
            identifier = await session.scalar(select(UserIdentifier))
            conversation = await session.scalar(select(Conversation))
            channel = await session.scalar(select(ConversationChannel))
            member = await session.scalar(select(ConversationMember))
            inbound = await session.scalar(select(Message).where(Message.direction == "inbound"))
            outbound = await session.scalar(select(Message).where(Message.direction == "outbound"))
            delivery = await session.scalar(select(MessageDelivery))
            run = await session.scalar(select(AgentRun))
            assert event is not None and event.status == "processed"
            assert user is not None and user.phone_number is None
            assert response.json()["user_id"] == str(user.id)
            assert user.onboarding_status == OnboardingStatus.COLLECTING_PROFILE.value
            assert user.onboarding_step == OnboardingStep.NAME.value
            assert identifier is not None
            assert identifier.user_id == user.id
            assert identifier.kind == UserIdentifierKind.EMAIL.value
            assert identifier.normalized_value == "person@example.com"
            assert identifier.source == "linq"
            assert identifier.verified_at is not None
            assert identifier.is_primary is True
            assert conversation is not None
            assert conversation.user_id == user.id
            assert conversation.kind == "direct"
            assert channel is not None
            assert channel.conversation_id == conversation.id
            assert channel.provider == "linq"
            assert channel.external_id == "chat-1"
            assert member is not None
            assert member.conversation_id == conversation.id
            assert member.user_id == user.id
            assert member.external_handle == "person@example.com"
            assert inbound is not None and inbound.content == "Hello"
            assert inbound.conversation_id == conversation.id
            assert inbound.user_id == user.id
            assert outbound is not None and outbound.status == "completed"
            assert outbound.conversation_id == conversation.id
            assert outbound.user_id == user.id
            assert delivery is not None and delivery.status == "sent"
            assert run is not None and run.purpose == AgentRunPurpose.ONBOARDING.value
            assert run.exposed_tools == []
            assert run.raw_output == {
                "messages": ["hey, i’m dot — save me to your contacts. what should i call you?"],
                "profile": fake_model.profile,
            }
            assert await session.scalar(select(func.count()).select_from(Conversation)) == 1
            assert await session.scalar(select(func.count()).select_from(ConversationChannel)) == 1
            assert await session.scalar(select(func.count()).select_from(Message)) == 2
        assert fake_model.structured_calls[0].name == "onboarding_turn"
        assert fake_linq.typing == [True, False]


@pytest.mark.anyio
async def test_linq_group_syncs_members_and_only_replies_when_invoked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    group_chat_id = "group-chat-1"
    async with linq_test_app(monkeypatch) as (
        client,
        session_factory,
        fake_linq,
        fake_model,
    ):
        fake_linq.chats[group_chat_id] = {
            "chat": {
                "id": group_chat_id,
                "display_name": "Safari crew",
                "is_group": True,
                "service": "iMessage",
                "handles": [
                    {
                        "id": "benji-handle",
                        "handle": "+16463038325",
                        "is_me": True,
                        "service": "iMessage",
                        "status": "active",
                    },
                    {
                        "id": "alice-handle",
                        "handle": "+14155552671",
                        "is_me": False,
                        "service": "iMessage",
                        "status": "active",
                    },
                    {
                        "id": "bob-handle",
                        "handle": "+14155552672",
                        "is_me": False,
                        "service": "iMessage",
                        "status": "active",
                    },
                    {
                        "id": "email-handle",
                        "handle": "group-member@example.com",
                        "is_me": False,
                        "service": "iMessage",
                        "status": "active",
                    },
                ],
            }
        }
        first_payload = message_received_payload(
            chat_id=group_chat_id,
            is_group=True,
            text="hello dot",
        )
        first_body = json.dumps(first_payload, separators=(",", ":")).encode()
        first = await client.post(
            "/api/v1/webhooks/linq",
            content=first_body,
            headers=signed_headers(first_body),
        )
        assert first.status_code == 200
        assert first.json()["reply_scheduled"] is True
        assert len(fake_linq.sent) == 1
        assert fake_linq.sent[0]["chat_id"] == group_chat_id
        assert fake_linq.read_chats == []
        assert fake_linq.typing == []  # Linq does not support typing in groups.
        assert fake_model.structured_calls == []
        assert fake_model.regular_tool_names == [
            "get_current_datetime",
            "create_personal_app",
        ]

        second_payload = message_received_payload(
            event_id="group-event-2",
            message_id="group-message-2",
            chat_id=group_chat_id,
            sender_phone="+14155552672",
            is_group=True,
            text="saturday works for me",
        )
        second_body = json.dumps(second_payload, separators=(",", ":")).encode()
        second = await client.post(
            "/api/v1/webhooks/linq",
            content=second_body,
            headers=signed_headers(second_body),
        )
        assert second.status_code == 200
        assert second.json()["reply_scheduled"] is True
        assert len(fake_linq.sent) == 1

        fake_model.group_should_respond = True
        fake_model.response_text = "yeah, i know a few spots. what part of town?"
        question_payload = message_received_payload(
            event_id="group-question-event",
            message_id="group-question-message",
            chat_id=group_chat_id,
            sender_phone="+14155552672",
            is_group=True,
            text="any good spots for six people?",
        )
        question_body = json.dumps(question_payload, separators=(",", ":")).encode()
        question_response = await client.post(
            "/api/v1/webhooks/linq",
            content=question_body,
            headers=signed_headers(question_body),
        )
        assert question_response.json()["reply_scheduled"] is True
        assert fake_linq.sent[-1]["text"] == "yeah, i know a few spots. what part of town?"
        fake_model.group_should_respond = False

        email_payload = message_received_payload(
            event_id="group-email-event",
            message_id="group-email-message",
            chat_id=group_chat_id,
            sender_phone="group-member@example.com",
            is_group=True,
            text="dot, can you help us choose?",
        )
        fake_model.response_text = (
            '{"messages":["yeah — i can help","what are the options?"],'
            '"follow_up":{"should_schedule":false,"goal":"",'
            '"due_after_seconds":0}}'
        )
        fake_model.group_acknowledgment = "yeah **gimme a sec**, checking now"
        email_body = json.dumps(email_payload, separators=(",", ":")).encode()
        email_response = await client.post(
            "/api/v1/webhooks/linq",
            content=email_body,
            headers=signed_headers(email_body),
        )
        assert email_response.status_code == 200
        assert email_response.json()["reply_scheduled"] is True
        assert len(fake_linq.sent) == 5
        assert [message["text"] for message in fake_linq.sent[-3:]] == [
            "yeah gimme a sec, checking now",
            "yeah, i can help",
            "what are the options?",
        ]
        assert fake_model.regular_tool_names == [
            "get_current_datetime",
            "create_personal_app",
        ]

        renamed_payload = {
            **message_received_payload(event_id="unused"),
            "event_type": "chat.group_name_updated",
            "event_id": "group-event-3",
            "data": {
                "chat_id": group_chat_id,
                "old_value": "Safari crew",
                "new_value": "Road trip crew",
            },
        }
        renamed_body = json.dumps(renamed_payload, separators=(",", ":")).encode()
        renamed = await client.post(
            "/api/v1/webhooks/linq",
            content=renamed_body,
            headers=signed_headers(renamed_body),
        )
        assert renamed.status_code == 200

        async with session_factory() as session:
            group = await session.scalar(select(Conversation).where(Conversation.kind == "group"))
            assert group is not None
            assert group.title == "Road trip crew"
            members = (
                await session.scalars(
                    select(ConversationMember).where(ConversationMember.conversation_id == group.id)
                )
            ).all()
            messages = (
                await session.scalars(
                    select(Message)
                    .where(
                        Message.conversation_id == group.id,
                        Message.direction == "inbound",
                    )
                    .order_by(Message.created_at)
                )
            ).all()
        assert len(members) == 3
        assert len(messages) == 4
        assert messages[0].sender_user_id is not None
        assert messages[1].sender_user_id is not None
        assert messages[0].sender_user_id != messages[1].sender_user_id
        assert messages[3].sender_user_id is not None
        assert messages[3].raw_payload["_sender_label"] == "an unnamed group member"
        async with session_factory() as session:
            email_identifier = await session.scalar(
                select(UserIdentifier).where(
                    UserIdentifier.kind == UserIdentifierKind.EMAIL.value,
                    UserIdentifier.normalized_value == "group-member@example.com",
                )
            )
        assert email_identifier is not None
        assert email_identifier.user_id == messages[3].sender_user_id
        owner = next(member for member in members if member.role == "owner")
        assert group.group_owner_source == "first_invoker"
        assert owner.user_id == messages[0].sender_user_id


@pytest.mark.anyio
async def test_adding_benji_to_existing_group_introduces_him_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    group_chat_id = "existing-group-chat"
    payload = {
        **message_received_payload(event_id="benji-added-event"),
        "event_type": "participant.added",
        "data": {
            "chat_id": group_chat_id,
            "participant": {
                "id": "benji-handle",
                "handle": "+16463038325",
                "is_me": True,
                "service": "iMessage",
            },
        },
    }
    body = json.dumps(payload, separators=(",", ":")).encode()

    async with linq_test_app(monkeypatch) as (
        client,
        session_factory,
        fake_linq,
        _,
    ):
        fake_linq.chats[group_chat_id] = {
            "chat": {
                "id": group_chat_id,
                "display_name": "Weekend plans",
                "is_group": True,
                "service": "iMessage",
                "handles": [
                    {
                        "id": "benji-handle",
                        "handle": "+16463038325",
                        "is_me": True,
                        "service": "iMessage",
                        "status": "active",
                    },
                    {
                        "id": "alice-handle",
                        "handle": "+14155552671",
                        "is_me": False,
                        "service": "iMessage",
                        "status": "active",
                    },
                    {
                        "id": "bob-handle",
                        "handle": "+14155552672",
                        "is_me": False,
                        "service": "iMessage",
                        "status": "active",
                    },
                ],
            }
        }

        first = await client.post(
            "/api/v1/webhooks/linq",
            content=body,
            headers=signed_headers(body),
        )
        duplicate = await client.post(
            "/api/v1/webhooks/linq",
            content=body,
            headers=signed_headers(body),
        )

        assert first.status_code == 200
        assert first.json()["reply_scheduled"] is True
        assert duplicate.json()["duplicate"] is True
        assert len(fake_linq.sent) == 1
        assert fake_linq.sent[0]["chat_id"] == group_chat_id
        assert fake_linq.typing == []

        ordinary = message_received_payload(
            event_id="ordinary-group-event",
            message_id="ordinary-group-message",
            chat_id=group_chat_id,
            sender_phone="+14155552672",
            is_group=True,
            text="saturday works for me",
        )
        ordinary_body = json.dumps(ordinary, separators=(",", ":")).encode()
        ordinary_response = await client.post(
            "/api/v1/webhooks/linq",
            content=ordinary_body,
            headers=signed_headers(ordinary_body),
        )
        assert ordinary_response.json()["reply_scheduled"] is True
        assert len(fake_linq.sent) == 1

        invoked = message_received_payload(
            event_id="invoked-group-event",
            message_id="invoked-group-message",
            chat_id=group_chat_id,
            sender_phone="+14155552672",
            is_group=True,
            text="dot, help us pick a time",
        )
        invoked_body = json.dumps(invoked, separators=(",", ":")).encode()
        invoked_response = await client.post(
            "/api/v1/webhooks/linq",
            content=invoked_body,
            headers=signed_headers(invoked_body),
        )
        assert invoked_response.json()["reply_scheduled"] is True
        assert len(fake_linq.sent) == 2

        async with session_factory() as session:
            group = await session.scalar(select(Conversation).where(Conversation.kind == "group"))
            event = await session.scalar(
                select(UserEvent).where(UserEvent.event_type == "group.dot_added")
            )
            assert group is not None and group.title == "Weekend plans"
            assert event is not None
            assert event.conversation_id == group.id
            assert event.status == UserEventStatus.PROCESSED.value


@pytest.mark.anyio
async def test_newly_discovered_linq_group_introduces_dot_from_chat_created(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    group_chat_id = "newly-discovered-group"
    chat = {
        "id": group_chat_id,
        "display_name": "Book club",
        "is_group": True,
        "service": "iMessage",
        "handles": [
            {
                "id": "dot-handle",
                "handle": "+16463038325",
                "is_me": True,
                "service": "iMessage",
                "status": "active",
            },
            {
                "id": "owner-handle",
                "handle": "reader@example.com",
                "is_me": False,
                "service": "iMessage",
                "status": "active",
            },
        ],
    }
    payload = {
        **message_received_payload(event_id="chat-created-event"),
        "event_type": "chat.created",
        "data": chat,
    }
    body = json.dumps(payload, separators=(",", ":")).encode()

    async with linq_test_app(monkeypatch) as (
        client,
        session_factory,
        fake_linq,
        _,
    ):
        fake_linq.chats[group_chat_id] = {"chat": chat}
        response = await client.post(
            "/api/v1/webhooks/linq",
            content=body,
            headers=signed_headers(body),
        )
        duplicate = await client.post(
            "/api/v1/webhooks/linq",
            content=body,
            headers=signed_headers(body),
        )

        assert response.status_code == 200
        assert response.json()["reply_scheduled"] is True
        assert duplicate.json()["duplicate"] is True
        assert len(fake_linq.sent) == 1
        assert fake_linq.sent[0]["chat_id"] == group_chat_id
        assert fake_linq.typing == []
        async with session_factory() as session:
            group = await session.scalar(select(Conversation).where(Conversation.kind == "group"))
            event = await session.scalar(
                select(UserEvent).where(UserEvent.event_type == "group.dot_added")
            )
            identifier = await session.scalar(
                select(UserIdentifier).where(
                    UserIdentifier.normalized_value == "reader@example.com"
                )
            )
            assert group is not None and group.title == "Book club"
            assert event is not None and event.conversation_id == group.id
            assert identifier is not None and identifier.user_id == group.user_id


@pytest.mark.anyio
async def test_opt_out_stops_automated_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    body = json.dumps(message_received_payload(text="STOP"), separators=(",", ":")).encode()

    async with linq_test_app(monkeypatch) as (
        client,
        session_factory,
        fake_linq,
        fake_model,
    ):
        response = await client.post(
            "/api/v1/webhooks/linq", content=body, headers=signed_headers(body)
        )

        assert response.status_code == 200
        assert response.json()["reply_scheduled"] is False
        assert fake_linq.sent == []
        async with session_factory() as session:
            user = await session.scalar(select(User))
            channel = await session.scalar(select(ConversationChannel))
        assert user is not None and user.messaging_opted_out_at is not None
        assert channel is not None and channel.status == "opted_out"
        assert fake_model.structured_calls == []


@pytest.mark.anyio
async def test_invalid_linq_signature_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = json.dumps(message_received_payload(), separators=(",", ":")).encode()

    async with linq_test_app(monkeypatch) as (client, _, fake_linq, _):
        response = await client.post(
            "/api/v1/webhooks/linq",
            content=body,
            headers=signed_headers(body, valid=False),
        )

        assert response.status_code == 401
        assert fake_linq.sent == []


@pytest.mark.anyio
async def test_completed_onboarding_unlocks_tools_on_the_next_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with linq_test_app(monkeypatch) as (
        client,
        session_factory,
        fake_linq,
        fake_model,
    ):
        fake_model.profile = {
            "display_name": "Kareem",
            "birth_date": "1992-04-18",
            "location_city": "Cairo",
            "location_country": "Egypt",
        }
        first_body = json.dumps(
            message_received_payload(text="i’m Kareem, born April 18 1992 in Cairo"),
            separators=(",", ":"),
        ).encode()
        first = await client.post(
            "/api/v1/webhooks/linq",
            content=first_body,
            headers=signed_headers(first_body),
        )

        second_body = json.dumps(
            message_received_payload(
                event_id="event-2",
                message_id="message-2",
                text="what can you do?",
            ),
            separators=(",", ":"),
        ).encode()
        second = await client.post(
            "/api/v1/webhooks/linq",
            content=second_body,
            headers=signed_headers(second_body),
        )

        assert first.status_code == 200
        assert second.status_code == 200
        assert len(fake_model.structured_calls) == 1
        assert fake_model.regular_tool_names == [
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
            "get_account_settings",
            "update_account_setting",
            "delete_dot_account",
            "cancel_account_deletion",
            "create_personal_app",
            "list_personal_apps",
            "inspect_custom_app",
            "create_custom_app_link",
            "list_custom_app_records",
            "add_custom_app_record",
            "update_custom_app_record",
            "delete_custom_app_record",
            "revise_custom_app",
            "rollback_custom_app",
            "delete_personal_app",
            "get_personal_app",
            "add_personal_app_record",
            "update_personal_app_record",
            "delete_personal_app_record",
        ]
        assert len(fake_linq.sent) == 2
        async with session_factory() as session:
            user = await session.scalar(select(User))
            runs = (await session.scalars(select(AgentRun).order_by(AgentRun.started_at))).all()
        assert user is not None
        assert user.onboarding_step == OnboardingStep.COMPLETE.value
        assert user.location_text == "Cairo, Egypt"
        assert [run.purpose for run in runs] == [
            AgentRunPurpose.ONBOARDING.value,
            AgentRunPurpose.CONVERSATION.value,
        ]


@pytest.mark.anyio
async def test_linq_media_is_persisted_once_and_reaches_the_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_url = "https://cdn.linqapp.com/attachments/partners/partner-1/attachment-1/photo.jpg"
    payload = message_received_payload(
        parts=[
            {
                "type": "media",
                "id": "attachment-1",
                "filename": "photo.jpg",
                "mime_type": "image/jpeg",
                "size_bytes": 123_456,
                "url": image_url,
            }
        ]
    )
    body = json.dumps(payload, separators=(",", ":")).encode()

    async with linq_test_app(monkeypatch) as (
        client,
        session_factory,
        _,
        fake_model,
    ):
        response = await client.post(
            "/api/v1/webhooks/linq",
            content=body,
            headers=signed_headers(body),
        )
        duplicate = await client.post(
            "/api/v1/webhooks/linq",
            content=body,
            headers=signed_headers(body),
        )

        assert response.status_code == 200
        assert duplicate.json()["duplicate"] is True
        async with session_factory() as session:
            message = await session.scalar(select(Message))
            attachments = (await session.scalars(select(MessageAttachment))).all()
        assert message is not None and message.content == "[sent an image]"
        assert len(attachments) == 1
        assert attachments[0].provider_attachment_id == "attachment-1"
        assert attachments[0].source_url == image_url
        assert attachments[0].source_url_expires_at is None
        model_attachment = fake_model.structured_messages[-1][-1].attachments[0]
        assert model_attachment.kind == "image"
        assert model_attachment.url == image_url


def test_legacy_linq_media_payload_is_parsed() -> None:
    envelope = LinqWebhookEnvelope.model_validate(
        {
            "api_version": "v3",
            "webhook_version": "2025-01-01",
            "event_type": "message.received",
            "event_id": "legacy-event",
            "created_at": "2026-08-11T12:00:00Z",
            "data": {
                "chat_id": "legacy-chat",
                "from_handle": {"handle": "person@example.com", "service": "iMessage"},
                "is_group": True,
                "message": {
                    "id": "legacy-message",
                    "parts": [
                        {"type": "text", "value": "look at this"},
                        {
                            "type": "media",
                            "id": "legacy-attachment",
                            "content_type": "image/png",
                            "url": "https://cdn.linqapp.com/temporary/image.png",
                        },
                    ],
                },
            },
        }
    )

    inbound = LinqInboundMessage.from_envelope(envelope)

    assert inbound.external_chat_id == "legacy-chat"
    assert inbound.external_message_id == "legacy-message"
    assert inbound.is_group is True
    assert inbound.text == "look at this"
    assert inbound.attachments[0].provider_attachment_id == "legacy-attachment"
    assert inbound.attachments[0].mime_type == "image/png"
