"""Add multipart assistant turns and durable agent follow-ups.

Revision ID: 20260810_0011
Revises: 20260809_0010
Create Date: 2026-08-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260810_0011"
down_revision: str | None = "20260809_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("response_group_id", sa.Uuid(), nullable=True))
    op.add_column("messages", sa.Column("response_ordinal", sa.Integer(), nullable=True))
    op.create_index(
        "ix_messages_response_group",
        "messages",
        ["response_group_id", "response_ordinal"],
    )

    op.alter_column("agent_runs", "trigger_message_id", nullable=True)
    op.add_column("agent_runs", sa.Column("trigger_event_id", sa.Uuid(), nullable=True))
    op.add_column(
        "agent_runs",
        sa.Column("wake_type", sa.String(length=64), nullable=False, server_default="user_message"),
    )
    op.create_foreign_key(
        "fk_agent_runs_trigger_event_id_user_events",
        "agent_runs",
        "user_events",
        ["trigger_event_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.alter_column("agent_runs", "wake_type", server_default=None)

    op.create_table(
        "agent_follow_ups",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("source_agent_run_id", sa.Uuid(), nullable=False),
        sa.Column("goal", sa.Text(), nullable=False),
        sa.Column("delivery_provider", sa.String(length=32), nullable=True),
        sa.Column("cancel_on_user_message", sa.Boolean(), nullable=False),
        sa.Column("chain_depth", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["source_agent_run_id"], ["agent_runs.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_agent_run_id"),
    )
    op.create_index("ix_agent_follow_ups_user_id", "agent_follow_ups", ["user_id"])
    op.create_index(
        "ix_agent_follow_ups_status_due", "agent_follow_ups", ["status", "due_at"]
    )
    op.create_index(
        "ix_agent_follow_ups_conversation_status",
        "agent_follow_ups",
        ["conversation_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_agent_follow_ups_conversation_status", table_name="agent_follow_ups")
    op.drop_index("ix_agent_follow_ups_status_due", table_name="agent_follow_ups")
    op.drop_index("ix_agent_follow_ups_user_id", table_name="agent_follow_ups")
    op.drop_table("agent_follow_ups")
    op.drop_constraint(
        "fk_agent_runs_trigger_event_id_user_events", "agent_runs", type_="foreignkey"
    )
    op.drop_column("agent_runs", "wake_type")
    op.drop_column("agent_runs", "trigger_event_id")
    op.alter_column("agent_runs", "trigger_message_id", nullable=False)
    op.drop_index("ix_messages_response_group", table_name="messages")
    op.drop_column("messages", "response_ordinal")
    op.drop_column("messages", "response_group_id")
