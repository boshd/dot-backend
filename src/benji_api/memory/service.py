import logging
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import and_, delete, exists, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from benji_api.agents.types import AgentMessage, ModelProvider
from benji_api.config import Settings
from benji_api.db.session import async_session_factory
from benji_api.memory.consolidation import (
    MEMORY_CONSOLIDATION_OUTPUT,
    MemoryConsolidation,
    MemoryOperation,
    build_memory_consolidation_instructions,
    contains_restricted_secret,
    parse_memory_consolidation,
)
from benji_api.memory.types import EmbeddingProvider, MemoryContext, RetrievedMemory
from benji_api.models.agent import AgentRun, AgentToolCall, ToolCallStatus
from benji_api.models.channel import Message, MessageDirection
from benji_api.models.memory import (
    MemoryEntity,
    MemoryEpisode,
    MemoryEvidence,
    MemoryFact,
    MemoryFactStatus,
    MemoryJob,
    MemoryJobStatus,
)
from benji_api.models.user import User

logger = logging.getLogger(__name__)
_MEMORY_EVIDENCE_TOOL_NAMES = frozenset(
    {
        "cancel_financial_goal",
        "cancel_scheduled_reachout",
        "create_financial_goal",
        "create_personal_app",
        "disconnect_financial_connection",
        "schedule_proactive_reachout",
    }
)
_MIN_SEMANTIC_RELEVANCE = 0.35
_MIN_LEXICAL_RELEVANCE = 0.20


def _is_relevant_fact(at: datetime) -> Any:
    return and_(
        or_(
            MemoryFact.status == MemoryFactStatus.ACTIVE.value,
            and_(
                MemoryFact.status == MemoryFactStatus.SUPERSEDED.value,
                MemoryFact.valid_until > at,
            ),
        ),
        or_(MemoryFact.valid_until.is_(None), MemoryFact.valid_until > at),
    )


@dataclass(frozen=True, slots=True)
class _FactCandidate:
    fact: MemoryFact
    subject_name: str
    object_name: str | None


@dataclass(frozen=True, slots=True)
class _JobInput:
    job: MemoryJob
    user: User
    trigger: Message
    responses: tuple[Message, ...]
    existing_facts: list[dict[str, str]]
    verified_tool_results: list[dict[str, Any]]


async def enqueue_memory_job(
    session: AsyncSession,
    *,
    user_id: UUID,
    conversation_id: UUID,
    trigger_message_id: UUID,
    response_message_id: UUID,
    idempotency_key: str,
) -> MemoryJob:
    existing = await session.scalar(
        select(MemoryJob).where(MemoryJob.idempotency_key == idempotency_key)
    )
    if existing is not None:
        return existing
    job = MemoryJob(
        user_id=user_id,
        conversation_id=conversation_id,
        trigger_message_id=trigger_message_id,
        response_message_id=response_message_id,
        idempotency_key=idempotency_key,
    )
    session.add(job)
    await session.flush()
    return job


async def consolidate_next_memory_job(
    *,
    settings: Settings,
    model_provider: ModelProvider,
    embedding_provider: EmbeddingProvider,
    job_id: UUID | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> bool:
    factory = session_factory or async_session_factory
    job = await _claim_job(
        factory,
        max_attempts=settings.memory_worker_max_attempts,
        job_id=job_id,
    )
    if job is None:
        return False

    try:
        job_input = await _load_job_input(factory, job.id)
        if job_input is None:
            await _finish_job(
                factory,
                job.id,
                status=MemoryJobStatus.SKIPPED,
                error="Memory job source data no longer exists",
            )
            return True
        result = await model_provider.generate_structured(
            instructions=build_memory_consolidation_instructions(
                user_name=job_input.user.display_name,
                existing_facts=job_input.existing_facts,
                verified_tool_results=job_input.verified_tool_results,
            ),
            messages=[
                AgentMessage(role="user", content=job_input.trigger.content),
                *(
                    AgentMessage(role="assistant", content=response.content)
                    for response in job_input.responses
                ),
            ],
            output=MEMORY_CONSOLIDATION_OUTPUT,
        )
        consolidation = _guard_consolidation(
            parse_memory_consolidation(result.data),
            user_text=job_input.trigger.content,
            has_verified_tool_results=bool(job_input.verified_tool_results),
        )
        texts = _texts_to_embed(consolidation)
        vectors = await embedding_provider.embed(texts)
        embeddings = dict(zip(texts, vectors, strict=True))
        stored = await _persist_consolidation(
            factory,
            job_input=job_input,
            consolidation=consolidation,
            embeddings=embeddings,
        )
        await _finish_job(
            factory,
            job.id,
            status=(MemoryJobStatus.PROCESSED if stored else MemoryJobStatus.SKIPPED),
        )
    except Exception as error:
        logger.exception("Memory job %s failed", job.id)
        await _retry_job(factory, job.id, error)
    return True


async def retrieve_memory_context(
    session: AsyncSession,
    *,
    user_id: UUID,
    query: str,
    embedding_provider: EmbeddingProvider | None,
    limit: int,
    candidate_limit: int,
) -> MemoryContext:
    if limit <= 0:
        return MemoryContext()
    now = datetime.now(UTC)
    query_embedding = await _safe_query_embedding(embedding_provider, query)
    subject = aliased(MemoryEntity)
    object_entity = aliased(MemoryEntity)
    base_statement = (
        select(MemoryFact, subject.name, object_entity.name)
        .join(subject, MemoryFact.subject_entity_id == subject.id)
        .outerjoin(object_entity, MemoryFact.object_entity_id == object_entity.id)
        .where(
            MemoryFact.user_id == user_id,
            _is_relevant_fact(now),
        )
    )
    rows = list(
        (
            await session.execute(
                base_statement.order_by(
                    MemoryFact.importance.desc(), MemoryFact.updated_at.desc()
                ).limit(max(candidate_limit, limit))
            )
        ).all()
    )
    if session.get_bind().dialect.name == "postgresql":
        if query_embedding is not None:
            rows.extend(
                (
                    await session.execute(
                        base_statement.where(MemoryFact.embedding.is_not(None))
                        .order_by(MemoryFact.embedding.cosine_distance(query_embedding))
                        .limit(candidate_limit)
                    )
                ).all()
            )
        if query.strip():
            text_query = func.plainto_tsquery("simple", query)
            text_vector = func.to_tsvector("simple", MemoryFact.statement)
            rows.extend(
                (
                    await session.execute(
                        base_statement.where(text_vector.op("@@")(text_query))
                        .order_by(func.ts_rank_cd(text_vector, text_query).desc())
                        .limit(candidate_limit)
                    )
                ).all()
            )
    unique_rows: dict[UUID, Any] = {row[0].id: row for row in rows}
    candidates = [
        _FactCandidate(fact=row[0], subject_name=row[1], object_name=row[2])
        for row in unique_rows.values()
    ]
    ranked = sorted(
        candidates,
        key=lambda candidate: _fact_score(candidate.fact, query, query_embedding, now),
        reverse=True,
    )
    relevant_ranked = [
        candidate
        for candidate in ranked
        if _passes_relevance_floor(candidate.fact, query, query_embedding)
    ]
    direct_candidate_ids = {candidate.fact.id for candidate in relevant_ranked}
    seed_limit = min(limit, max(1, limit // 2))
    selected = relevant_ranked[:seed_limit]
    selected = await _expand_graph(
        session,
        user_id=user_id,
        selected=selected,
        now=now,
        limit=limit,
    )
    if len(selected) < limit:
        selected_ids = {candidate.fact.id for candidate in selected}
        for candidate in relevant_ranked[seed_limit:]:
            if candidate.fact.id in selected_ids:
                continue
            selected.append(candidate)
            selected_ids.add(candidate.fact.id)
            if len(selected) >= limit:
                break

    episode_limit = min(3, max(0, limit - len(selected)))
    episodes: list[str] = []
    episode_rows: list[MemoryEpisode] = []
    if episode_limit:
        episode_rows = list(
            (
                await session.scalars(
                    select(MemoryEpisode)
                    .where(
                        MemoryEpisode.user_id == user_id,
                        MemoryEpisode.is_retrievable.is_(True),
                    )
                    .order_by(MemoryEpisode.occurred_at.desc())
                    .limit(candidate_limit)
                )
            ).all()
        )
        if query_embedding is not None and session.get_bind().dialect.name == "postgresql":
            vector_episodes = list(
                (
                    await session.scalars(
                        select(MemoryEpisode)
                        .where(
                            MemoryEpisode.user_id == user_id,
                            MemoryEpisode.is_retrievable.is_(True),
                            MemoryEpisode.embedding.is_not(None),
                        )
                        .order_by(MemoryEpisode.embedding.cosine_distance(query_embedding))
                        .limit(candidate_limit)
                    )
                ).all()
            )
            episode_rows = list(
                {episode.id: episode for episode in [*episode_rows, *vector_episodes]}.values()
            )
        episode_rows = [
            episode
            for episode in sorted(
                episode_rows,
                key=lambda item: _episode_score(item, query, query_embedding, now),
                reverse=True,
            )
            if _passes_episode_relevance_floor(episode, query, query_embedding)
        ][:episode_limit]
        episodes = [episode.summary for episode in episode_rows]

    retrieved = [
        RetrievedMemory(
            memory_id=str(candidate.fact.id),
            memory_type="fact",
            text=_format_fact(candidate),
            score=_fact_score(candidate.fact, query, query_embedding, now),
            relevance_score=_fact_relevance_score(candidate.fact, query, query_embedding),
            retrieval_reason=("direct" if candidate.fact.id in direct_candidate_ids else "graph"),
        )
        for candidate in selected
    ]
    retrieved.extend(
        RetrievedMemory(
            memory_id=str(episode.id),
            memory_type="episode",
            text=episode.summary,
            score=_episode_score(episode, query, query_embedding, now),
            relevance_score=_episode_relevance_score(episode, query, query_embedding),
        )
        for episode in episode_rows
    )

    return MemoryContext(
        facts=tuple(_format_fact(candidate) for candidate in selected),
        episodes=tuple(episodes),
        retrieved=tuple(retrieved),
    )


async def list_user_memories(
    session: AsyncSession,
    *,
    user_id: UUID,
    query: str | None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    now = datetime.now(UTC)
    statement = select(MemoryFact).where(
        MemoryFact.user_id == user_id,
        _is_relevant_fact(now),
    )
    if query and query.strip():
        terms = _tokens(query)
        if terms:
            statement = statement.where(
                or_(*(MemoryFact.statement.ilike(f"%{term}%") for term in terms))
            )
    facts = list(
        (
            await session.scalars(
                statement.order_by(
                    MemoryFact.importance.desc(), MemoryFact.updated_at.desc()
                ).limit(max(1, min(limit, 50)))
            )
        ).all()
    )
    return [
        {
            "id": str(fact.id),
            "kind": fact.kind,
            "statement": fact.statement,
            "confidence": fact.confidence,
            "valid_until": fact.valid_until.isoformat() if fact.valid_until else None,
        }
        for fact in facts
    ]


async def forget_user_memories(
    session: AsyncSession,
    *,
    user_id: UUID,
    memory_ids: list[UUID],
) -> int:
    if not memory_ids:
        return 0
    facts = list(
        (
            await session.scalars(
                select(MemoryFact).where(
                    MemoryFact.user_id == user_id,
                    MemoryFact.id.in_(memory_ids),
                )
            )
        ).all()
    )
    if not facts:
        return 0
    fact_ids = [fact.id for fact in facts]
    episode_ids = list(
        (
            await session.scalars(
                select(MemoryEvidence.episode_id).where(MemoryEvidence.fact_id.in_(fact_ids))
            )
        ).all()
    )
    if episode_ids:
        await session.execute(
            update(MemoryEpisode)
            .where(MemoryEpisode.id.in_(episode_ids))
            .values(
                summary="[forgotten by user]",
                is_retrievable=False,
                embedding=None,
            )
        )
    await session.execute(delete(MemoryFact).where(MemoryFact.id.in_(fact_ids)))
    referenced_as_subject = exists(
        select(MemoryFact.id).where(MemoryFact.subject_entity_id == MemoryEntity.id)
    )
    referenced_as_object = exists(
        select(MemoryFact.id).where(MemoryFact.object_entity_id == MemoryEntity.id)
    )
    await session.execute(
        delete(MemoryEntity).where(
            MemoryEntity.user_id == user_id,
            ~referenced_as_subject,
            ~referenced_as_object,
        )
    )
    await session.commit()
    return len(facts)


async def _claim_job(
    factory: async_sessionmaker[AsyncSession],
    *,
    max_attempts: int,
    job_id: UUID | None,
) -> MemoryJob | None:
    now = datetime.now(UTC)
    stale_before = now - timedelta(minutes=5)
    eligible = or_(
        MemoryJob.status == MemoryJobStatus.PENDING.value,
        and_(
            MemoryJob.status == MemoryJobStatus.FAILED.value,
            MemoryJob.next_attempt_at <= now,
        ),
        and_(
            MemoryJob.status == MemoryJobStatus.PROCESSING.value,
            MemoryJob.locked_at <= stale_before,
        ),
    )
    async with factory() as session:
        statement = (
            select(MemoryJob)
            .where(eligible, MemoryJob.attempts < max_attempts)
            .order_by(MemoryJob.next_attempt_at, MemoryJob.created_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if job_id is not None:
            statement = statement.where(MemoryJob.id == job_id)
        job = await session.scalar(statement)
        if job is None:
            return None
        job.status = MemoryJobStatus.PROCESSING.value
        job.attempts += 1
        job.locked_at = now
        job.error = None
        await session.commit()
        return job


async def _load_job_input(
    factory: async_sessionmaker[AsyncSession], job_id: UUID
) -> _JobInput | None:
    async with factory() as session:
        job = await session.get(MemoryJob, job_id)
        if job is None:
            return None
        user = await session.get(User, job.user_id)
        trigger = await session.get(Message, job.trigger_message_id)
        response = await session.get(Message, job.response_message_id)
        if user is None or trigger is None or response is None:
            return None
        responses = [response]
        if response.response_group_id is not None:
            responses = list(
                (
                    await session.scalars(
                        select(Message)
                        .where(
                            Message.conversation_id == job.conversation_id,
                            Message.user_id == job.user_id,
                            Message.response_group_id == response.response_group_id,
                            Message.direction == MessageDirection.OUTBOUND.value,
                        )
                        .order_by(Message.response_ordinal, Message.created_at, Message.id)
                    )
                ).all()
            )
        subject = aliased(MemoryEntity)
        object_entity = aliased(MemoryEntity)
        rows = (
            await session.execute(
                select(MemoryFact, subject.name, object_entity.name)
                .join(subject, MemoryFact.subject_entity_id == subject.id)
                .outerjoin(object_entity, MemoryFact.object_entity_id == object_entity.id)
                .where(
                    MemoryFact.user_id == job.user_id,
                    _is_relevant_fact(datetime.now(UTC)),
                )
                .order_by(MemoryFact.importance.desc(), MemoryFact.updated_at.desc())
                .limit(100)
            )
        ).all()
        existing_facts = [
            {
                "id": str(row[0].id),
                "subject": row[1],
                "predicate": row[0].predicate,
                "object": row[2] or row[0].object_value or "",
                "statement": row[0].statement,
                "valid_from": row[0].valid_from.isoformat(),
                "valid_until": (row[0].valid_until.isoformat() if row[0].valid_until else ""),
            }
            for row in rows
        ]
        tool_results = await _load_verified_tool_results(session, response)
        return _JobInput(
            job=job,
            user=user,
            trigger=trigger,
            responses=tuple(responses),
            existing_facts=existing_facts,
            verified_tool_results=tool_results,
        )


async def _load_verified_tool_results(
    session: AsyncSession, response: Message
) -> list[dict[str, Any]]:
    raw_run_id = response.raw_payload.get("agent_run_id")
    if not isinstance(raw_run_id, str):
        return []
    try:
        run_id = UUID(raw_run_id)
    except ValueError:
        return []
    run = await session.get(AgentRun, run_id)
    if run is None or run.user_id != response.user_id:
        return []
    calls = list(
        (
            await session.scalars(
                select(AgentToolCall).where(
                    AgentToolCall.agent_run_id == run.id,
                    AgentToolCall.status == ToolCallStatus.COMPLETED.value,
                )
            )
        ).all()
    )
    return [
        {
            "tool": call.tool_name,
            "arguments": _sanitize_tool_evidence(call.arguments),
            "output": _sanitize_tool_evidence(call.output),
        }
        for call in calls
        if call.tool_name in _MEMORY_EVIDENCE_TOOL_NAMES
    ]


def _sanitize_tool_evidence(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _sanitize_tool_evidence(item)
            for key, item in value.items()
            if not any(
                marker in str(key).casefold()
                for marker in ("access_token", "ciphertext", "password", "secret", "token", "url")
            )
        }
    if isinstance(value, list):
        return [_sanitize_tool_evidence(item) for item in value]
    if isinstance(value, str):
        return value[:1_000]
    return value


def _guard_consolidation(
    consolidation: MemoryConsolidation,
    *,
    user_text: str,
    has_verified_tool_results: bool,
) -> MemoryConsolidation:
    explicitly_requested = bool(
        re.search(
            r"\b(remember|don't forget|do not forget|keep (?:this|that) in mind)\b",
            user_text,
            re.IGNORECASE,
        )
    )
    operations = tuple(
        operation
        for operation in consolidation.operations
        if operation.sensitivity != "restricted"
        and not contains_restricted_secret(operation.statement)
        and (operation.sensitivity != "sensitive" or explicitly_requested)
        and (operation.source_basis == "user_stated" or has_verified_tool_results)
    )
    return MemoryConsolidation(
        store_episode=consolidation.store_episode,
        episode_summary=consolidation.episode_summary,
        operations=operations,
    )


def _texts_to_embed(consolidation: MemoryConsolidation) -> list[str]:
    texts: list[str] = []
    if consolidation.store_episode and consolidation.episode_summary:
        texts.append(consolidation.episode_summary)
    for operation in consolidation.operations:
        if operation.action in {"add", "supersede"} and operation.statement not in texts:
            texts.append(operation.statement)
    return texts


async def _persist_consolidation(
    factory: async_sessionmaker[AsyncSession],
    *,
    job_input: _JobInput,
    consolidation: MemoryConsolidation,
    embeddings: dict[str, list[float]],
) -> bool:
    if not consolidation.operations and not (
        consolidation.store_episode and consolidation.episode_summary
    ):
        return False
    async with factory() as session:
        existing_episode = await session.scalar(
            select(MemoryEpisode).where(MemoryEpisode.job_id == job_input.job.id)
        )
        if existing_episode is not None:
            return True
        summary = consolidation.episode_summary or (
            consolidation.operations[0].statement if consolidation.operations else ""
        )
        episode = MemoryEpisode(
            user_id=job_input.job.user_id,
            conversation_id=job_input.job.conversation_id,
            job_id=job_input.job.id,
            trigger_message_id=job_input.trigger.id,
            response_message_id=job_input.responses[-1].id,
            summary=summary,
            is_retrievable=consolidation.store_episode,
            embedding=embeddings.get(consolidation.episode_summary),
            occurred_at=_as_utc(job_input.trigger.created_at),
        )
        session.add(episode)
        await session.flush()
        for operation in consolidation.operations:
            await _apply_operation(
                session,
                user_id=job_input.job.user_id,
                operation=operation,
                episode=episode,
                trigger_message_id=job_input.trigger.id,
                response_message_id=job_input.responses[-1].id,
                occurred_at=_as_utc(job_input.trigger.created_at),
                embedding=embeddings.get(operation.statement),
            )
        await session.commit()
        return True


async def _apply_operation(
    session: AsyncSession,
    *,
    user_id: UUID,
    operation: MemoryOperation,
    episode: MemoryEpisode,
    trigger_message_id: UUID,
    response_message_id: UUID,
    occurred_at: datetime,
    embedding: list[float] | None,
) -> None:
    existing = None
    if operation.existing_fact_id is not None:
        existing = await session.scalar(
            select(MemoryFact).where(
                MemoryFact.id == operation.existing_fact_id,
                MemoryFact.user_id == user_id,
                _is_relevant_fact(datetime.now(UTC)),
            )
        )
    if operation.action == "reinforce":
        if existing is None:
            return
        existing.confidence = max(existing.confidence, operation.confidence)
        existing.importance = max(existing.importance, operation.importance)
        await _add_evidence(
            session,
            fact=existing,
            episode=episode,
            source_message_id=(
                trigger_message_id
                if operation.source_basis == "user_stated"
                else response_message_id
            ),
            evidence_type=operation.source_basis,
        )
        return

    subject = await _resolve_entity(
        session,
        user_id=user_id,
        name=operation.subject_name,
        entity_type=operation.subject_type,
    )
    object_entity = None
    if operation.object_is_entity and operation.object_name and operation.object_type:
        object_entity = await _resolve_entity(
            session,
            user_id=user_id,
            name=operation.object_name,
            entity_type=operation.object_type,
        )
    duplicate = await session.scalar(
        select(MemoryFact).where(
            MemoryFact.user_id == user_id,
            MemoryFact.subject_entity_id == subject.id,
            MemoryFact.predicate == operation.predicate,
            MemoryFact.status == MemoryFactStatus.ACTIVE.value,
            (
                MemoryFact.object_entity_id == object_entity.id
                if object_entity is not None
                else and_(
                    MemoryFact.object_entity_id.is_(None),
                    MemoryFact.object_value == operation.object_value,
                )
            ),
        )
    )
    if duplicate is not None:
        duplicate.confidence = max(duplicate.confidence, operation.confidence)
        duplicate.importance = max(duplicate.importance, operation.importance)
        await _add_evidence(
            session,
            fact=duplicate,
            episode=episode,
            source_message_id=trigger_message_id,
            evidence_type=operation.source_basis,
        )
        return

    fact = MemoryFact(
        user_id=user_id,
        subject_entity_id=subject.id,
        predicate=operation.predicate,
        object_entity_id=object_entity.id if object_entity else None,
        object_value=None if object_entity else operation.object_value,
        statement=operation.statement,
        kind=operation.kind,
        confidence=operation.confidence,
        importance=operation.importance,
        sensitivity=operation.sensitivity,
        valid_from=operation.valid_from or occurred_at,
        valid_until=operation.valid_until,
        embedding=embedding,
        metadata_={"source_basis": operation.source_basis},
    )
    session.add(fact)
    await session.flush()
    if operation.action == "supersede" and existing is not None:
        existing.status = MemoryFactStatus.SUPERSEDED.value
        existing.valid_until = (
            operation.valid_from
            if operation.valid_from and operation.valid_from > occurred_at
            else occurred_at
        )
        existing.superseded_by_fact_id = fact.id
    await _add_evidence(
        session,
        fact=fact,
        episode=episode,
        source_message_id=(
            trigger_message_id if operation.source_basis == "user_stated" else response_message_id
        ),
        evidence_type=operation.source_basis,
    )


async def _resolve_entity(
    session: AsyncSession,
    *,
    user_id: UUID,
    name: str,
    entity_type: str,
) -> MemoryEntity:
    canonical_key = _canonical_key(name)
    entity = await session.scalar(
        select(MemoryEntity).where(
            MemoryEntity.user_id == user_id,
            MemoryEntity.entity_type == entity_type,
            MemoryEntity.canonical_key == canonical_key,
        )
    )
    if entity is None:
        entity = MemoryEntity(
            user_id=user_id,
            entity_type=entity_type,
            name=name,
            canonical_key=canonical_key,
            aliases=[],
        )
        session.add(entity)
        await session.flush()
    elif name != entity.name and name not in entity.aliases:
        entity.aliases = [*entity.aliases, name]
    return entity


async def _add_evidence(
    session: AsyncSession,
    *,
    fact: MemoryFact,
    episode: MemoryEpisode,
    source_message_id: UUID,
    evidence_type: str,
) -> None:
    existing = await session.scalar(
        select(MemoryEvidence).where(
            MemoryEvidence.fact_id == fact.id,
            MemoryEvidence.episode_id == episode.id,
        )
    )
    if existing is None:
        session.add(
            MemoryEvidence(
                fact_id=fact.id,
                episode_id=episode.id,
                source_message_id=source_message_id,
                evidence_type=evidence_type,
            )
        )


async def _expand_graph(
    session: AsyncSession,
    *,
    user_id: UUID,
    selected: list[_FactCandidate],
    now: datetime,
    limit: int,
) -> list[_FactCandidate]:
    if not selected or len(selected) >= limit:
        return selected
    entity_ids = {
        entity_id
        for candidate in selected[:3]
        for entity_id in _expansion_entity_ids(candidate)
        if entity_id is not None
    }
    if not entity_ids:
        return selected
    subject = aliased(MemoryEntity)
    object_entity = aliased(MemoryEntity)
    rows = (
        await session.execute(
            select(MemoryFact, subject.name, object_entity.name)
            .join(subject, MemoryFact.subject_entity_id == subject.id)
            .outerjoin(object_entity, MemoryFact.object_entity_id == object_entity.id)
            .where(
                MemoryFact.user_id == user_id,
                _is_relevant_fact(now),
                or_(
                    MemoryFact.subject_entity_id.in_(entity_ids),
                    MemoryFact.object_entity_id.in_(entity_ids),
                ),
            )
            .order_by(MemoryFact.importance.desc())
            .limit(limit)
        )
    ).all()
    seen = {candidate.fact.id for candidate in selected}
    expanded = list(selected)
    for row in rows:
        if row[0].id in seen:
            continue
        expanded.append(_FactCandidate(fact=row[0], subject_name=row[1], object_name=row[2]))
        seen.add(row[0].id)
        if len(expanded) >= limit:
            break
    return expanded


async def _safe_query_embedding(
    provider: EmbeddingProvider | None, query: str
) -> list[float] | None:
    if provider is None or not query.strip():
        return None
    try:
        vectors = await provider.embed([query.strip()])
        return vectors[0] if vectors else None
    except Exception:
        logger.warning("Memory query embedding failed; using lexical fallback", exc_info=True)
        return None


def _fact_score(
    fact: MemoryFact,
    query: str,
    query_embedding: list[float] | None,
    now: datetime,
) -> float:
    semantic = _cosine(query_embedding, fact.embedding)
    lexical = _lexical_score(query, fact.statement)
    age_days = max((_as_utc(now) - _as_utc(fact.updated_at)).total_seconds(), 0) / 86_400
    recency = math.exp(-age_days / 180)
    return (
        0.55 * semantic
        + 0.20 * lexical
        + 0.15 * (fact.importance / 5)
        + 0.05 * fact.confidence
        + 0.05 * recency
    )


def _fact_relevance_score(
    fact: MemoryFact,
    query: str,
    query_embedding: list[float] | None,
) -> float:
    return max(
        max(_cosine(query_embedding, fact.embedding), 0.0),
        _lexical_score(query, fact.statement),
    )


def _passes_relevance_floor(
    fact: MemoryFact,
    query: str,
    query_embedding: list[float] | None,
) -> bool:
    semantic = max(_cosine(query_embedding, fact.embedding), 0.0)
    lexical = _lexical_score(query, fact.statement)
    return semantic >= _MIN_SEMANTIC_RELEVANCE or lexical >= _MIN_LEXICAL_RELEVANCE


def _episode_score(
    episode: MemoryEpisode,
    query: str,
    query_embedding: list[float] | None,
    now: datetime,
) -> float:
    semantic = _cosine(query_embedding, episode.embedding)
    lexical = _lexical_score(query, episode.summary)
    age_days = max((_as_utc(now) - _as_utc(episode.occurred_at)).total_seconds(), 0) / 86_400
    return 0.65 * semantic + 0.25 * lexical + 0.10 * math.exp(-age_days / 180)


def _episode_relevance_score(
    episode: MemoryEpisode,
    query: str,
    query_embedding: list[float] | None,
) -> float:
    return max(
        max(_cosine(query_embedding, episode.embedding), 0.0),
        _lexical_score(query, episode.summary),
    )


def _passes_episode_relevance_floor(
    episode: MemoryEpisode,
    query: str,
    query_embedding: list[float] | None,
) -> bool:
    semantic = max(_cosine(query_embedding, episode.embedding), 0.0)
    lexical = _lexical_score(query, episode.summary)
    return semantic >= _MIN_SEMANTIC_RELEVANCE or lexical >= _MIN_LEXICAL_RELEVANCE


def _expansion_entity_ids(candidate: _FactCandidate) -> tuple[UUID | None, ...]:
    # Most user memories share the synthetic "user" node. Expanding through it would turn one
    # relevant preference into every unrelated preference. Named objects and non-user subjects
    # still provide the useful graph hops (for example user -> Maya -> London).
    subject_id = (
        None
        if candidate.subject_name.casefold() in {"user", "the user"}
        else candidate.fact.subject_entity_id
    )
    return subject_id, candidate.fact.object_entity_id


def _format_fact(candidate: _FactCandidate) -> str:
    fact = candidate.fact
    valid_from = (
        f"; effective from {fact.valid_from.date().isoformat()}"
        if _as_utc(fact.valid_from) > datetime.now(UTC)
        else ""
    )
    valid_until = (
        f"; relevant until {fact.valid_until.date().isoformat()}" if fact.valid_until else ""
    )
    return f"{fact.statement}{valid_from}{valid_until}"


def _cosine(left: list[float] | None, right: Any) -> float:
    if left is None or right is None:
        return 0.0
    right_values = list(right)
    if len(left) != len(right_values):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right_values, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right_values))
    if not left_norm or not right_norm:
        return 0.0
    return max(-1.0, min(dot / (left_norm * right_norm), 1.0))


def _lexical_score(query: str, text: str) -> float:
    query_terms = _tokens(query)
    if not query_terms:
        return 0.0
    text_terms = _tokens(text)
    return len(query_terms & text_terms) / len(query_terms)


def _tokens(value: str) -> set[str]:
    stop_words = {
        "a",
        "an",
        "and",
        "are",
        "do",
        "i",
        "is",
        "it",
        "me",
        "my",
        "of",
        "the",
        "to",
        "what",
        "you",
    }
    return {
        token
        for token in re.findall(r"[\w'-]+", value.casefold())
        if len(token) > 1 and token not in stop_words
    }


def _canonical_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")[:255]


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


async def _finish_job(
    factory: async_sessionmaker[AsyncSession],
    job_id: UUID,
    *,
    status: MemoryJobStatus,
    error: str | None = None,
) -> None:
    async with factory() as session:
        job = await session.get(MemoryJob, job_id)
        if job is None:
            return
        job.status = status.value
        job.error = error
        job.locked_at = None
        job.processed_at = datetime.now(UTC)
        await session.commit()


async def _retry_job(
    factory: async_sessionmaker[AsyncSession], job_id: UUID, error: Exception
) -> None:
    async with factory() as session:
        job = await session.get(MemoryJob, job_id)
        if job is None:
            return
        delay_seconds = min(2 ** max(job.attempts, 1), 300)
        job.status = MemoryJobStatus.FAILED.value
        job.error = str(error)[:2_000]
        job.locked_at = None
        job.next_attempt_at = datetime.now(UTC) + timedelta(seconds=delay_seconds)
        await session.commit()
