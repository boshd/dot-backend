"""Add the temporal user-memory graph and durable consolidation jobs.

Revision ID: 20260809_0009
Revises: 20260809_0008
Create Date: 2026-08-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision: str = "20260809_0009"
down_revision: str | None = "20260809_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "memory_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("trigger_message_id", sa.Uuid(), nullable=False),
        sa.Column("response_message_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["response_message_id"], ["messages.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["trigger_message_id"], ["messages.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_memory_jobs_user_id", "memory_jobs", ["user_id"])
    op.create_index(
        "ix_memory_jobs_status_next_attempt", "memory_jobs", ["status", "next_attempt_at"]
    )
    op.create_index(
        "ix_memory_jobs_idempotency_key", "memory_jobs", ["idempotency_key"], unique=True
    )

    op.create_table(
        "memory_episodes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("trigger_message_id", sa.Uuid(), nullable=False),
        sa.Column("response_message_id", sa.Uuid(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("is_retrievable", sa.Boolean(), nullable=False),
        sa.Column("embedding", Vector(1536), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["job_id"], ["memory_jobs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["response_message_id"], ["messages.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["trigger_message_id"], ["messages.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", name="uq_memory_episodes_job_id"),
    )
    op.create_index(
        "ix_memory_episodes_user_occurred", "memory_episodes", ["user_id", "occurred_at"]
    )

    op.create_table(
        "memory_entities",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("entity_type", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("canonical_key", sa.String(length=255), nullable=False),
        sa.Column("aliases", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id", "entity_type", "canonical_key", name="uq_memory_entities_identity"
        ),
    )
    op.create_index("ix_memory_entities_user_id", "memory_entities", ["user_id"])

    op.create_table(
        "memory_facts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("subject_entity_id", sa.Uuid(), nullable=False),
        sa.Column("predicate", sa.String(length=128), nullable=False),
        sa.Column("object_entity_id", sa.Uuid(), nullable=True),
        sa.Column("object_value", sa.Text(), nullable=True),
        sa.Column("statement", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("importance", sa.Integer(), nullable=False),
        sa.Column("sensitivity", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("superseded_by_fact_id", sa.Uuid(), nullable=True),
        sa.Column("embedding", Vector(1536), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["object_entity_id"], ["memory_entities.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["subject_entity_id"], ["memory_entities.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["superseded_by_fact_id"], ["memory_facts.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_memory_facts_user_status", "memory_facts", ["user_id", "status"])
    op.create_index(
        "ix_memory_facts_subject_predicate",
        "memory_facts",
        ["subject_entity_id", "predicate"],
    )

    op.create_table(
        "memory_evidence",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("fact_id", sa.Uuid(), nullable=False),
        sa.Column("episode_id", sa.Uuid(), nullable=False),
        sa.Column("source_message_id", sa.Uuid(), nullable=True),
        sa.Column("evidence_type", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["episode_id"], ["memory_episodes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["fact_id"], ["memory_facts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_message_id"], ["messages.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("fact_id", "episode_id", name="uq_memory_evidence_fact_episode"),
    )
    op.create_index("ix_memory_evidence_episode_id", "memory_evidence", ["episode_id"])

    op.execute(
        "CREATE INDEX ix_memory_facts_embedding_hnsw ON memory_facts "
        "USING hnsw (embedding vector_cosine_ops) WHERE embedding IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX ix_memory_episodes_embedding_hnsw ON memory_episodes "
        "USING hnsw (embedding vector_cosine_ops) WHERE embedding IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX ix_memory_facts_statement_fts ON memory_facts "
        "USING gin (to_tsvector('english', statement))"
    )


def downgrade() -> None:
    op.drop_table("memory_evidence")
    op.drop_table("memory_facts")
    op.drop_table("memory_entities")
    op.drop_table("memory_episodes")
    op.drop_table("memory_jobs")
