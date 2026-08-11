from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from benji_api.agents.types import (
    AgentMessage,
    ModelSession,
    StructuredModelResult,
    StructuredOutputDefinition,
    ToolDefinition,
)
from benji_api.config import Settings
from benji_api.db.base import Base
from benji_api.memory.service import (
    consolidate_next_memory_job,
    enqueue_memory_job,
    forget_user_memories,
    retrieve_memory_context,
)
from benji_api.models import (
    AgentRun,
    AgentToolCall,
    Conversation,
    MemoryEntity,
    MemoryEpisode,
    MemoryEvidence,
    MemoryFact,
    MemoryFactStatus,
    MemoryJob,
    MemoryJobStatus,
    Message,
    MessageDirection,
    MessageStatus,
    ToolCallStatus,
    User,
)


class FakeMemoryModelProvider:
    name = "fake"
    model = "fake-memory-model"

    def __init__(self, data: dict[str, object]) -> None:
        self.data = data
        self.instructions = ""
        self.messages: list[AgentMessage] = []

    def start(
        self,
        *,
        instructions: str,
        messages: list[AgentMessage],
        tools: list[ToolDefinition],
        output: StructuredOutputDefinition | None = None,
    ) -> ModelSession:
        raise AssertionError("regular model sessions are not used by memory consolidation")

    async def generate_structured(
        self,
        *,
        instructions: str,
        messages: list[AgentMessage],
        output: StructuredOutputDefinition,
    ) -> StructuredModelResult:
        assert output.name == "memory_consolidation"
        assert "guarded personal-memory consolidator" in instructions
        assert messages[0].role == "user"
        assert all(message.role == "assistant" for message in messages[1:])
        self.instructions = instructions
        self.messages = messages
        return StructuredModelResult(response_id="memory-response", data=self.data)


class FakeEmbeddingProvider:
    name = "fake"
    model = "fake-embedding"
    dimensions = 1536

    async def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            lowered = text.casefold()
            vector = [0.0] * self.dimensions
            vector[0] = 1.0 if any(word in lowered for word in ("sister", "maya")) else 0.0
            vector[1] = 1.0 if any(word in lowered for word in ("live", "london")) else 0.0
            vector[2] = 1.0 if "paris" in lowered else 0.0
            vectors.append(vector)
        return vectors


def _operation(
    *,
    subject_name: str,
    subject_type: str,
    predicate: str,
    statement: str,
    object_name: str | None = None,
    object_type: str | None = None,
    object_value: str | None = None,
    action: str = "add",
    existing_fact_id: str | None = None,
    valid_from: datetime | None = None,
) -> dict[str, object]:
    return {
        "action": action,
        "existing_fact_id": existing_fact_id,
        "kind": "relationship" if predicate == "has_sister" else "biographical",
        "subject_name": subject_name,
        "subject_type": subject_type,
        "predicate": predicate,
        "object_is_entity": object_name is not None,
        "object_name": object_name,
        "object_type": object_type,
        "object_value": object_value,
        "statement": statement,
        "confidence": 0.98,
        "importance": 4,
        "sensitivity": "normal",
        "valid_from": valid_from.isoformat() if valid_from else None,
        "valid_until": None,
        "source_basis": "user_stated",
    }


async def _seed_turn(
    factory: async_sessionmaker[AsyncSession],
    *,
    user: User,
    conversation: Conversation,
    text: str,
    reply: str,
) -> MemoryJob:
    async with factory() as session:
        trigger = Message(
            conversation_id=conversation.id,
            user_id=user.id,
            source_channel="web",
            source_external_id=f"in-{text}",
            direction=MessageDirection.INBOUND.value,
            status=MessageStatus.RECEIVED.value,
            content=text,
        )
        response = Message(
            conversation_id=conversation.id,
            user_id=user.id,
            source_channel="web",
            idempotency_key=f"out-{text}",
            direction=MessageDirection.OUTBOUND.value,
            status=MessageStatus.COMPLETED.value,
            content=reply,
        )
        session.add_all([trigger, response])
        await session.flush()
        job = await enqueue_memory_job(
            session,
            user_id=user.id,
            conversation_id=conversation.id,
            trigger_message_id=trigger.id,
            response_message_id=response.id,
            idempotency_key=f"memory-{trigger.id}",
        )
        await session.commit()
        return job


@pytest.mark.anyio
async def test_memory_consolidation_builds_graph_and_retrieves_related_facts() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with factory() as session:
        user = User(phone_number="+14155552671", display_name="Kareem")
        session.add(user)
        await session.flush()
        conversation = Conversation(user_id=user.id)
        session.add(conversation)
        await session.commit()

    job = await _seed_turn(
        factory,
        user=user,
        conversation=conversation,
        text="my sister is maya and she lives in london",
        reply="got it",
    )
    provider = FakeMemoryModelProvider(
        {
            "store_episode": True,
            "episode_summary": "Kareem shared that his sister Maya lives in London.",
            "operations": [
                _operation(
                    subject_name="user",
                    subject_type="person",
                    predicate="has_sister",
                    object_name="Maya",
                    object_type="person",
                    statement="Kareem's sister is Maya.",
                ),
                _operation(
                    subject_name="Maya",
                    subject_type="person",
                    predicate="lives_in",
                    object_name="London",
                    object_type="place",
                    statement="Maya lives in London.",
                ),
            ],
        }
    )
    embedder = FakeEmbeddingProvider()
    handled = await consolidate_next_memory_job(
        settings=Settings(memory_enabled=True),
        model_provider=provider,
        embedding_provider=embedder,
        job_id=job.id,
        session_factory=factory,
    )
    assert handled is True

    async with factory() as session:
        persisted_job = await session.get(MemoryJob, job.id)
        assert persisted_job is not None
        assert persisted_job.status == MemoryJobStatus.PROCESSED.value
        assert await session.scalar(select(func.count()).select_from(MemoryEntity)) == 3
        assert await session.scalar(select(func.count()).select_from(MemoryFact)) == 2
        assert await session.scalar(select(func.count()).select_from(MemoryEvidence)) == 2
        assert await session.scalar(select(func.count()).select_from(MemoryEpisode)) == 1

        context = await retrieve_memory_context(
            session,
            user_id=user.id,
            query="where does my sister live?",
            embedding_provider=embedder,
            limit=4,
            candidate_limit=20,
        )
    assert "Kareem's sister is Maya." in context.facts
    assert "Maya lives in London." in context.facts
    assert context.episodes == ("Kareem shared that his sister Maya lives in London.",)
    assert {item.memory_type for item in context.retrieved} == {"fact", "episode"}
    assert all(item.score > 0 for item in context.retrieved)

    async with factory() as session:
        generic_context = await retrieve_memory_context(
            session,
            user_id=user.id,
            query="hi",
            embedding_provider=embedder,
            limit=4,
            candidate_limit=20,
        )
        graph_context = await retrieve_memory_context(
            session,
            user_id=user.id,
            query="tell me about my sister",
            embedding_provider=None,
            limit=4,
            candidate_limit=20,
        )
    assert generic_context.empty
    assert generic_context.retrieved == ()
    assert "Kareem's sister is Maya." in graph_context.facts
    assert "Maya lives in London." in graph_context.facts
    maya_location = next(
        item for item in graph_context.retrieved if item.text == "Maya lives in London."
    )
    assert maya_location.retrieval_reason == "graph"

    async with factory() as session:
        fact_ids = list((await session.scalars(select(MemoryFact.id))).all())
        deleted = await forget_user_memories(
            session,
            user_id=user.id,
            memory_ids=fact_ids,
        )
        assert deleted == 2
        assert await session.scalar(select(func.count()).select_from(MemoryFact)) == 0
        assert await session.scalar(select(func.count()).select_from(MemoryEntity)) == 0
        episode = await session.scalar(select(MemoryEpisode))
        assert episode is not None
        assert episode.summary == "[forgotten by user]"
        assert episode.is_retrievable is False
    await engine.dispose()


@pytest.mark.anyio
async def test_memory_consolidation_reads_all_bubbles_and_confirmed_action_results() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with factory() as session:
        user = User(phone_number="+14155552679", display_name="Kareem")
        session.add(user)
        await session.flush()
        conversation = Conversation(user_id=user.id)
        session.add(conversation)
        await session.flush()
        trigger = Message(
            conversation_id=conversation.id,
            user_id=user.id,
            source_channel="web",
            source_external_id="multi-bubble-trigger",
            direction=MessageDirection.INBOUND.value,
            status=MessageStatus.RECEIVED.value,
            content="make me a workout tracker",
        )
        session.add(trigger)
        await session.flush()
        run = AgentRun(
            conversation_id=conversation.id,
            user_id=user.id,
            trigger_message_id=trigger.id,
            provider="fake",
            model="fake-model",
            input_message_count=1,
        )
        session.add(run)
        await session.flush()
        response_group_id = uuid4()
        responses = [
            Message(
                conversation_id=conversation.id,
                user_id=user.id,
                source_channel="web",
                idempotency_key=f"multi-bubble:{ordinal}",
                response_group_id=response_group_id,
                response_ordinal=ordinal,
                direction=MessageDirection.OUTBOUND.value,
                status=MessageStatus.COMPLETED.value,
                content=text,
                raw_payload={"agent_run_id": str(run.id)},
            )
            for ordinal, text in enumerate(("yeah, i got you", "made it. here’s your tracker"))
        ]
        session.add_all(responses)
        session.add(
            AgentToolCall(
                agent_run_id=run.id,
                external_call_id="call-create-app",
                tool_name="create_personal_app",
                arguments={"title": "Workout tracker"},
                output={"ok": True, "result": {"url": "https://dot.test/apps/workout"}},
                status=ToolCallStatus.COMPLETED.value,
            )
        )
        await session.flush()
        job = await enqueue_memory_job(
            session,
            user_id=user.id,
            conversation_id=conversation.id,
            trigger_message_id=trigger.id,
            response_message_id=responses[-1].id,
            idempotency_key="memory-multi-bubble",
        )
        await session.commit()

    provider = FakeMemoryModelProvider(
        {"store_episode": False, "episode_summary": "", "operations": []}
    )
    await consolidate_next_memory_job(
        settings=Settings(memory_enabled=True),
        model_provider=provider,
        embedding_provider=FakeEmbeddingProvider(),
        job_id=job.id,
        session_factory=factory,
    )

    assert [message.content for message in provider.messages] == [
        "make me a workout tracker",
        "yeah, i got you",
        "made it. here’s your tracker",
    ]
    assert "create_personal_app" in provider.instructions
    assert "Workout tracker" in provider.instructions
    assert "https://dot.test/apps/workout" not in provider.instructions
    await engine.dispose()


@pytest.mark.anyio
async def test_memory_supersession_preserves_temporal_history() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with factory() as session:
        user = User(phone_number="+14155552672", display_name="Kareem")
        session.add(user)
        await session.flush()
        conversation = Conversation(user_id=user.id)
        session.add(conversation)
        await session.commit()

    first_job = await _seed_turn(
        factory,
        user=user,
        conversation=conversation,
        text="maya lives in london",
        reply="got it",
    )
    first_provider = FakeMemoryModelProvider(
        {
            "store_episode": False,
            "episode_summary": "",
            "operations": [
                _operation(
                    subject_name="Maya",
                    subject_type="person",
                    predicate="lives_in",
                    object_name="London",
                    object_type="place",
                    statement="Maya lives in London.",
                )
            ],
        }
    )
    embedder = FakeEmbeddingProvider()
    await consolidate_next_memory_job(
        settings=Settings(memory_enabled=True),
        model_provider=first_provider,
        embedding_provider=embedder,
        job_id=first_job.id,
        session_factory=factory,
    )
    async with factory() as session:
        old_fact = await session.scalar(select(MemoryFact))
        assert old_fact is not None
        old_fact_id = old_fact.id

    second_job = await _seed_turn(
        factory,
        user=user,
        conversation=conversation,
        text="maya moved to paris",
        reply="noted",
    )
    second_provider = FakeMemoryModelProvider(
        {
            "store_episode": True,
            "episode_summary": "Kareem said Maya moved from London to Paris.",
            "operations": [
                _operation(
                    action="supersede",
                    existing_fact_id=str(old_fact_id),
                    subject_name="Maya",
                    subject_type="person",
                    predicate="lives_in",
                    object_name="Paris",
                    object_type="place",
                    statement="Maya lives in Paris.",
                )
            ],
        }
    )
    await consolidate_next_memory_job(
        settings=Settings(memory_enabled=True),
        model_provider=second_provider,
        embedding_provider=embedder,
        job_id=second_job.id,
        session_factory=factory,
    )

    async with factory() as session:
        old_fact = await session.get(MemoryFact, old_fact_id)
        new_fact = await session.scalar(
            select(MemoryFact).where(MemoryFact.statement == "Maya lives in Paris.")
        )
        assert old_fact is not None and new_fact is not None
        assert old_fact.status == MemoryFactStatus.SUPERSEDED.value
        assert old_fact.valid_until is not None
        assert old_fact.superseded_by_fact_id == new_fact.id
        assert new_fact.status == MemoryFactStatus.ACTIVE.value
        assert new_fact.valid_from is not None
    await engine.dispose()


@pytest.mark.anyio
async def test_future_supersession_keeps_current_and_future_facts_retrievable() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with factory() as session:
        user = User(phone_number="+14155552673", display_name="Kareem")
        session.add(user)
        await session.flush()
        conversation = Conversation(user_id=user.id)
        session.add(conversation)
        await session.commit()

    first_job = await _seed_turn(
        factory,
        user=user,
        conversation=conversation,
        text="maya lives in london",
        reply="got it",
    )
    embedder = FakeEmbeddingProvider()
    await consolidate_next_memory_job(
        settings=Settings(memory_enabled=True),
        model_provider=FakeMemoryModelProvider(
            {
                "store_episode": False,
                "episode_summary": "",
                "operations": [
                    _operation(
                        subject_name="Maya",
                        subject_type="person",
                        predicate="lives_in",
                        object_name="London",
                        object_type="place",
                        statement="Maya lives in London.",
                    )
                ],
            }
        ),
        embedding_provider=embedder,
        job_id=first_job.id,
        session_factory=factory,
    )
    async with factory() as session:
        old_fact = await session.scalar(select(MemoryFact))
        assert old_fact is not None
        old_fact_id = old_fact.id

    effective_at = datetime.now(UTC) + timedelta(days=30)
    second_job = await _seed_turn(
        factory,
        user=user,
        conversation=conversation,
        text="maya is moving to paris next month",
        reply="noted",
    )
    await consolidate_next_memory_job(
        settings=Settings(memory_enabled=True),
        model_provider=FakeMemoryModelProvider(
            {
                "store_episode": False,
                "episode_summary": "",
                "operations": [
                    _operation(
                        action="supersede",
                        existing_fact_id=str(old_fact_id),
                        subject_name="Maya",
                        subject_type="person",
                        predicate="lives_in",
                        object_name="Paris",
                        object_type="place",
                        statement="Maya will live in Paris next month.",
                        valid_from=effective_at,
                    )
                ],
            }
        ),
        embedding_provider=embedder,
        job_id=second_job.id,
        session_factory=factory,
    )

    async with factory() as session:
        context = await retrieve_memory_context(
            session,
            user_id=user.id,
            query="where does maya live?",
            embedding_provider=embedder,
            limit=4,
            candidate_limit=20,
        )
    assert any("Maya lives in London." in fact for fact in context.facts)
    assert any("Maya will live in Paris next month." in fact for fact in context.facts)
    assert any("effective from" in fact for fact in context.facts)
    assert any("relevant until" in fact for fact in context.facts)
    await engine.dispose()
