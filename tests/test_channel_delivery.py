from typing import Any

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from benji_api.agents.channel_delivery import (
    deliver_linq_replies,
    inter_bubble_typing_delay,
)
from benji_api.agents.results import PersistedReply
from benji_api.db.base import Base
from benji_api.models.channel import (
    Conversation,
    ConversationChannel,
    Message,
    MessageDirection,
    MessageStatus,
)
from benji_api.models.user import User


class TimelineLinqClient:
    def __init__(self, timeline: list[str]) -> None:
        self.timeline = timeline
        self.sent = 0

    async def send_chat_message(
        self,
        *,
        chat_id: str,
        text: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        del chat_id, idempotency_key
        self.sent += 1
        self.timeline.append(f"send:{text}")
        return {"id": f"message-{self.sent}"}

    async def start_typing(self, *, chat_id: str) -> None:
        del chat_id
        self.timeline.append("typing:on")

    async def stop_typing(self, *, chat_id: str) -> None:
        del chat_id
        self.timeline.append("typing:off")


def test_inter_bubble_typing_delay_is_length_aware_bounded_and_disableable() -> None:
    assert (
        inter_bubble_typing_delay(
            "anything",
            minimum_seconds=0,
            seconds_per_character=1,
            maximum_seconds=3,
        )
        == 0
    )
    assert (
        inter_bubble_typing_delay(
            "12345",
            minimum_seconds=0.5,
            seconds_per_character=0.1,
            maximum_seconds=3,
        )
        == 1
    )
    assert (
        inter_bubble_typing_delay(
            "x" * 100,
            minimum_seconds=0.5,
            seconds_per_character=0.1,
            maximum_seconds=3,
        )
        == 3
    )


@pytest.mark.anyio
async def test_multipart_linq_delivery_restarts_typing_before_each_later_bubble(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async with factory() as session:
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
        )
        session.add(channel)
        messages = [
            Message(
                conversation_id=conversation.id,
                user_id=user.id,
                source_channel="linq",
                direction=MessageDirection.OUTBOUND.value,
                status=MessageStatus.COMPLETED.value,
                content=text,
            )
            for text in ("one", "second bubble", "a considerably longer third bubble")
        ]
        session.add_all(messages)
        await session.commit()

    monkeypatch.setattr(
        "benji_api.agents.channel_delivery.async_session_factory",
        factory,
    )
    timeline: list[str] = []

    async def fake_sleep(delay: float) -> None:
        timeline.append(f"sleep:{delay:.2f}")

    client = TimelineLinqClient(timeline)
    replies = tuple(
        PersistedReply(message_id=message.id, text=message.content) for message in messages
    )
    await deliver_linq_replies(
        replies=replies,
        channel_id=channel.id,
        chat_id="chat-1",
        idempotency_key="turn-1",
        client=client,  # type: ignore[arg-type]
        inter_message_delay_seconds=0.5,
        typing_between_messages=True,
        typing_seconds_per_character=0.1,
        typing_max_delay_seconds=3,
        sleep=fake_sleep,
    )

    assert timeline == [
        "send:one",
        "typing:on",
        "sleep:1.80",
        "send:second bubble",
        "typing:on",
        "sleep:3.00",
        "send:a considerably longer third bubble",
    ]

    timeline.clear()
    await deliver_linq_replies(
        replies=replies,
        channel_id=channel.id,
        chat_id="chat-1",
        idempotency_key="turn-1",
        client=client,  # type: ignore[arg-type]
        inter_message_delay_seconds=0.5,
        typing_between_messages=True,
        typing_seconds_per_character=0.1,
        typing_max_delay_seconds=3,
        sleep=fake_sleep,
    )
    assert timeline == []

    await engine.dispose()
