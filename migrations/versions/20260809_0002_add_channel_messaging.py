"""Add provider-neutral channel messaging and webhook persistence.

Revision ID: 20260809_0002
Revises: 20260809_0001
Create Date: 2026-08-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260809_0002"
down_revision: str | None = "20260809_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users", sa.Column("messaging_opted_out_at", sa.DateTime(timezone=True), nullable=True)
    )

    op.create_table(
        "channel_conversations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=False),
        sa.Column("service", sa.String(length=32), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "provider", "external_id", name="uq_channel_conversations_provider_id"
        ),
    )
    op.create_index(
        "ix_channel_conversations_user_id", "channel_conversations", ["user_id"], unique=False
    )

    op.create_table(
        "channel_messages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
        sa.Column("direction", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("raw_payload", sa.JSON(), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["channel_conversations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider", "external_id", name="uq_channel_messages_provider_id"),
        sa.UniqueConstraint(
            "provider", "idempotency_key", name="uq_channel_messages_provider_idempotency"
        ),
    )
    op.create_index(
        "ix_channel_messages_conversation_id",
        "channel_messages",
        ["conversation_id"],
        unique=False,
    )
    op.create_index(
        "ix_channel_messages_user_id", "channel_messages", ["user_id"], unique=False
    )

    op.create_table(
        "webhook_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("external_event_id", sa.String(length=255), nullable=False),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("trace_id", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider", "external_event_id", name="uq_webhook_events_provider_id"),
    )
    op.create_index("ix_webhook_events_status", "webhook_events", ["status"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_webhook_events_status", table_name="webhook_events")
    op.drop_table("webhook_events")
    op.drop_index("ix_channel_messages_user_id", table_name="channel_messages")
    op.drop_index("ix_channel_messages_conversation_id", table_name="channel_messages")
    op.drop_table("channel_messages")
    op.drop_index("ix_channel_conversations_user_id", table_name="channel_conversations")
    op.drop_table("channel_conversations")
    op.drop_column("users", "messaging_opted_out_at")
