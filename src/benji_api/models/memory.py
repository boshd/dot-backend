from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from benji_api.db.base import Base
from benji_api.models.user import utc_now

MEMORY_EMBEDDING_DIMENSIONS = 1536
MemoryVector = Vector(MEMORY_EMBEDDING_DIMENSIONS).with_variant(JSON(), "sqlite")


class MemoryJobStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    PROCESSED = "processed"
    FAILED = "failed"
    SKIPPED = "skipped"


class MemoryFactStatus(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    EXPIRED = "expired"


class MemoryJob(Base):
    __tablename__ = "memory_jobs"
    __table_args__ = (
        Index("ix_memory_jobs_user_id", "user_id"),
        Index("ix_memory_jobs_status_next_attempt", "status", "next_attempt_at"),
        Index("ix_memory_jobs_idempotency_key", "idempotency_key", unique=True),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    trigger_message_id: Mapped[UUID] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    response_message_id: Mapped[UUID] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), default=MemoryJobStatus.PENDING.value, nullable=False
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class MemoryEpisode(Base):
    __tablename__ = "memory_episodes"
    __table_args__ = (
        UniqueConstraint("job_id", name="uq_memory_episodes_job_id"),
        Index("ix_memory_episodes_user_occurred", "user_id", "occurred_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    job_id: Mapped[UUID] = mapped_column(
        ForeignKey("memory_jobs.id", ondelete="CASCADE"), nullable=False
    )
    trigger_message_id: Mapped[UUID] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    response_message_id: Mapped[UUID] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    is_retrievable: Mapped[bool] = mapped_column(default=False, nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(MemoryVector)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class MemoryEntity(Base):
    __tablename__ = "memory_entities"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "entity_type", "canonical_key", name="uq_memory_entities_identity"
        ),
        Index("ix_memory_entities_user_id", "user_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    canonical_key: Mapped[str] = mapped_column(String(255), nullable=False)
    aliases: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class MemoryFact(Base):
    __tablename__ = "memory_facts"
    __table_args__ = (
        Index("ix_memory_facts_user_status", "user_id", "status"),
        Index("ix_memory_facts_subject_predicate", "subject_entity_id", "predicate"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    subject_entity_id: Mapped[UUID] = mapped_column(
        ForeignKey("memory_entities.id", ondelete="CASCADE"), nullable=False
    )
    predicate: Mapped[str] = mapped_column(String(128), nullable=False)
    object_entity_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("memory_entities.id", ondelete="SET NULL")
    )
    object_value: Mapped[str | None] = mapped_column(Text)
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    importance: Mapped[int] = mapped_column(Integer, nullable=False)
    sensitivity: Mapped[str] = mapped_column(String(32), default="normal", nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), default=MemoryFactStatus.ACTIVE.value, nullable=False
    )
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    superseded_by_fact_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("memory_facts.id", ondelete="SET NULL")
    )
    embedding: Mapped[list[float] | None] = mapped_column(MemoryVector)
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSON, default=dict, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class MemoryEvidence(Base):
    __tablename__ = "memory_evidence"
    __table_args__ = (
        UniqueConstraint("fact_id", "episode_id", name="uq_memory_evidence_fact_episode"),
        Index("ix_memory_evidence_episode_id", "episode_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    fact_id: Mapped[UUID] = mapped_column(
        ForeignKey("memory_facts.id", ondelete="CASCADE"), nullable=False
    )
    episode_id: Mapped[UUID] = mapped_column(
        ForeignKey("memory_episodes.id", ondelete="CASCADE"), nullable=False
    )
    source_message_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL")
    )
    evidence_type: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


Index(
    "ix_memory_facts_embedding_hnsw",
    MemoryFact.embedding,
    postgresql_using="hnsw",
    postgresql_ops={"embedding": "vector_cosine_ops"},
    postgresql_where=MemoryFact.embedding.is_not(None),
).ddl_if(dialect="postgresql")
Index(
    "ix_memory_episodes_embedding_hnsw",
    MemoryEpisode.embedding,
    postgresql_using="hnsw",
    postgresql_ops={"embedding": "vector_cosine_ops"},
    postgresql_where=MemoryEpisode.embedding.is_not(None),
).ddl_if(dialect="postgresql")
