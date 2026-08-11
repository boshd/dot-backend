from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from benji_api.agents.prompts.relationship import build_relationship_module
from benji_api.agents.relationship import (
    RelationshipState,
    is_generic_opening,
    is_identity_question,
    is_social_acknowledgment,
    load_relationship_state,
)
from benji_api.db.base import Base
from benji_api.models.channel import (
    Conversation,
    Message,
    MessageDirection,
    MessageStatus,
)
from benji_api.models.generated_app import GeneratedApp, GeneratedAppRecord
from benji_api.models.memory import MemoryEntity, MemoryFact
from benji_api.models.user import User


def test_relationship_turn_classification_is_narrow() -> None:
    assert is_generic_opening("Yooo") is True
    assert is_generic_opening("hey can you check my calendar") is False
    assert is_identity_question("so what are you basically?") is True
    assert is_identity_question("what are you doing tomorrow?") is False
    assert is_social_acknowledgment("No worries") is True
    assert is_social_acknowledgment("cool, can you check my calendar?") is False


@pytest.mark.anyio
async def test_relationship_state_surfaces_recent_unused_shared_work() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    now = datetime(2026, 8, 10, 15, 30, tzinfo=UTC)
    async with factory() as session:
        user = User(phone_number="+14155552671", display_name="Kareem")
        session.add(user)
        await session.flush()
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
                predicate="wants_to_run_marathon",
                object_value="run a marathon",
                statement="Kareem wants to run a marathon.",
                kind="goal",
                confidence=0.95,
                importance=5,
                valid_from=now - timedelta(days=1),
            )
        )
        conversation = Conversation(user_id=user.id)
        session.add(conversation)
        await session.flush()
        previous = Message(
            conversation_id=conversation.id,
            user_id=user.id,
            source_channel="linq",
            direction=MessageDirection.OUTBOUND.value,
            status=MessageStatus.COMPLETED.value,
            content="your tracker is ready",
            created_at=now - timedelta(hours=7),
        )
        trigger = Message(
            conversation_id=conversation.id,
            user_id=user.id,
            source_channel="linq",
            direction=MessageDirection.INBOUND.value,
            status=MessageStatus.RECEIVED.value,
            content="Hi",
            created_at=now,
        )
        app = GeneratedApp(
            user_id=user.id,
            conversation_id=conversation.id,
            public_id="advanced-gym",
            title="advanced gym workout tracker",
            description="track splits",
            template="metric_tracker",
            theme="ember",
            access_mode="private_link",
            created_at=now - timedelta(hours=7),
        )
        used_app = GeneratedApp(
            user_id=user.id,
            conversation_id=conversation.id,
            public_id="shared-expenses",
            title="shared expenses",
            description="split a trip",
            template="expense_splitter",
            theme="ocean",
            access_mode="collaborative_link",
            created_at=now - timedelta(hours=8),
        )
        session.add_all([previous, trigger, app, used_app])
        await session.flush()
        session.add(
            GeneratedAppRecord(
                app_id=used_app.id,
                module_id="expenses",
                kind="expense",
                data={"amount": 10},
                created_at=now - timedelta(hours=6),
            )
        )
        await session.commit()

        state = await load_relationship_state(
            session,
            user_id=user.id,
            conversation_id=conversation.id,
            trigger=trigger,
            now=now,
        )

    assert state.previous_message_at == previous.created_at
    assert state.recent_artifacts[0].title == "advanced gym workout tracker"
    assert state.recent_artifacts[0].record_count == 0
    assert state.recent_artifacts[1].record_count == 1
    assert state.active_commitments[-1].title == "Kareem wants to run a marathon."

    module = build_relationship_module(state, latest_user_text="Hi", now=now)
    normalized = " ".join(module.content.split())
    assert "about 7 hours ago" in normalized
    assert "advanced gym workout tracker" in normalized
    assert "more present than a generic what's-up question" in normalized

    identity = build_relationship_module(
        state,
        latest_user_text="what are you basically?",
        now=now,
    )
    identity_prompt = " ".join(identity.content.split())
    assert "single selected shared artifact" in identity_prompt
    assert "shared expenses" not in identity_prompt
    assert "mention no other example or capability" in identity_prompt
    await engine.dispose()


@pytest.mark.anyio
async def test_post_onboarding_handoff_is_injected_once() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    now = datetime(2026, 8, 10, 17, 0, tzinfo=UTC)
    async with factory() as session:
        user = User(phone_number="+14155552671", display_name="Kareem")
        session.add(user)
        await session.flush()
        conversation = Conversation(user_id=user.id)
        session.add(conversation)
        await session.flush()
        onboarding_reply = Message(
            conversation_id=conversation.id,
            user_id=user.id,
            source_channel="linq",
            direction=MessageDirection.OUTBOUND.value,
            status=MessageStatus.COMPLETED.value,
            content="fair. enough questions.",
            raw_payload={"onboarding_completed": True},
            created_at=now - timedelta(seconds=5),
        )
        trigger = Message(
            conversation_id=conversation.id,
            user_id=user.id,
            source_channel="linq",
            direction=MessageDirection.INBOUND.value,
            status=MessageStatus.RECEIVED.value,
            content="No worries",
            created_at=now,
        )
        session.add_all([onboarding_reply, trigger])
        await session.commit()

        first_state = await load_relationship_state(
            session,
            user_id=user.id,
            conversation_id=conversation.id,
            trigger=trigger,
            now=now,
        )
        assert first_state.onboarding_handoff_pending is True

        module = build_relationship_module(
            first_state,
            latest_user_text=trigger.content,
            now=now,
        )
        normalized = " ".join(module.content.split())
        assert "don't merely acknowledge the social beat and end" in normalized
        assert "not another round of questions" in normalized
        assert "without supplying a list of possible answers" in normalized

        normal_reply = Message(
            conversation_id=conversation.id,
            user_id=user.id,
            source_channel="linq",
            direction=MessageDirection.OUTBOUND.value,
            status=MessageStatus.COMPLETED.value,
            content="what made you text me in the first place?",
            raw_payload={},
            created_at=now + timedelta(seconds=1),
        )
        next_trigger = Message(
            conversation_id=conversation.id,
            user_id=user.id,
            source_channel="linq",
            direction=MessageDirection.INBOUND.value,
            status=MessageStatus.RECEIVED.value,
            content="mostly curious",
            created_at=now + timedelta(seconds=2),
        )
        session.add_all([normal_reply, next_trigger])
        await session.commit()
        next_state = await load_relationship_state(
            session,
            user_id=user.id,
            conversation_id=conversation.id,
            trigger=next_trigger,
            now=next_trigger.created_at,
        )
        assert next_state.onboarding_handoff_pending is False

    await engine.dispose()


def test_substantive_first_post_onboarding_message_is_not_redirected() -> None:
    state = RelationshipState(onboarding_handoff_pending=True)

    module = build_relationship_module(
        state,
        latest_user_text="check my calendar for tomorrow",
    )

    normalized = " ".join(module.content.split())
    assert "respond to it directly and advance it" in normalized
    assert "redirecting into a discovery script" in normalized
