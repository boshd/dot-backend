"""Add durable scheduling and provider-neutral financial data.

Revision ID: 20260810_0017
Revises: 20260810_0016
Create Date: 2026-08-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260810_0017"
down_revision: str | None = "20260810_0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "scheduled_tasks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.Column("action_type", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("title", sa.String(length=160), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("delivery_provider", sa.String(length=32), nullable=True),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("recurrence", sa.String(length=16), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("run_count", sa.Integer(), nullable=False),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_scheduled_tasks_user_id", "scheduled_tasks", ["user_id"])
    op.create_index(
        "ix_scheduled_tasks_status_attempt",
        "scheduled_tasks",
        ["status", "next_attempt_at"],
    )
    op.create_index(
        "ix_scheduled_tasks_conversation_status",
        "scheduled_tasks",
        ["conversation_id", "status"],
    )
    op.create_index(
        "ix_scheduled_tasks_idempotency_key",
        "scheduled_tasks",
        ["idempotency_key"],
        unique=True,
    )

    op.create_table(
        "financial_connections",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("provider_connection_id", sa.String(length=255), nullable=False),
        sa.Column("institution_id", sa.String(length=255), nullable=True),
        sa.Column("institution_name", sa.String(length=255), nullable=False),
        sa.Column("credentials_ciphertext", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("sync_status", sa.String(length=24), nullable=False),
        sa.Column("sync_cursor", sa.Text(), nullable=True),
        sa.Column("consent_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_sync_error", sa.Text(), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "provider", "provider_connection_id", name="uq_financial_connection_provider_id"
        ),
    )
    op.create_index(
        "ix_financial_connections_user_id", "financial_connections", ["user_id"]
    )
    op.create_index(
        "ix_financial_connections_status", "financial_connections", ["status"]
    )

    op.create_table(
        "financial_link_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=True),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("exchange_token_hash", sa.String(length=64), nullable=False),
        sa.Column("initiated_channel", sa.String(length=32), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["connection_id"], ["financial_connections.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_financial_link_sessions_token_hash",
        "financial_link_sessions",
        ["exchange_token_hash"],
        unique=True,
    )
    op.create_index(
        "ix_financial_link_sessions_user_id", "financial_link_sessions", ["user_id"]
    )

    op.create_table(
        "financial_accounts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("provider_account_id", sa.String(length=255), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("official_name", sa.String(length=255), nullable=True),
        sa.Column("mask", sa.String(length=16), nullable=True),
        sa.Column("account_type", sa.String(length=64), nullable=False),
        sa.Column("account_subtype", sa.String(length=64), nullable=True),
        sa.Column("currency", sa.String(length=16), nullable=True),
        sa.Column("current_balance", sa.Numeric(precision=20, scale=4), nullable=True),
        sa.Column("available_balance", sa.Numeric(precision=20, scale=4), nullable=True),
        sa.Column("hidden", sa.Boolean(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["connection_id"], ["financial_connections.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "connection_id",
            "provider_account_id",
            name="uq_financial_account_connection_id",
        ),
    )
    op.create_index(
        "ix_financial_accounts_connection_id", "financial_accounts", ["connection_id"]
    )

    op.create_table(
        "financial_transactions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("provider_transaction_id", sa.String(length=255), nullable=False),
        sa.Column("pending_transaction_id", sa.String(length=255), nullable=True),
        sa.Column("amount", sa.Numeric(precision=20, scale=4), nullable=False),
        sa.Column("currency", sa.String(length=16), nullable=True),
        sa.Column("transaction_date", sa.Date(), nullable=False),
        sa.Column("authorized_date", sa.Date(), nullable=True),
        sa.Column("name", sa.String(length=500), nullable=False),
        sa.Column("merchant_name", sa.String(length=255), nullable=True),
        sa.Column("pending", sa.Boolean(), nullable=False),
        sa.Column("category_json", sa.JSON(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["financial_accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source", "provider_transaction_id", name="uq_financial_transaction_source_id"
        ),
    )
    op.create_index(
        "ix_financial_transactions_account_date",
        "financial_transactions",
        ["account_id", "transaction_date"],
    )
    op.create_index(
        "ix_financial_transactions_user_date",
        "financial_transactions",
        ["user_id", "transaction_date"],
    )

    op.create_table(
        "financial_goals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("schedule_id", sa.Uuid(), nullable=True),
        sa.Column("title", sa.String(length=160), nullable=False),
        sa.Column("target_amount", sa.Numeric(precision=20, scale=4), nullable=False),
        sa.Column("currency", sa.String(length=16), nullable=False),
        sa.Column("target_date", sa.Date(), nullable=False),
        sa.Column("baseline_amount", sa.Numeric(precision=20, scale=4), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["conversations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["schedule_id"], ["scheduled_tasks.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_financial_goals_user_status", "financial_goals", ["user_id", "status"]
    )
    op.create_index(
        "ix_financial_goals_schedule_id", "financial_goals", ["schedule_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_financial_goals_schedule_id", table_name="financial_goals")
    op.drop_index("ix_financial_goals_user_status", table_name="financial_goals")
    op.drop_table("financial_goals")
    op.drop_index("ix_financial_transactions_user_date", table_name="financial_transactions")
    op.drop_index("ix_financial_transactions_account_date", table_name="financial_transactions")
    op.drop_table("financial_transactions")
    op.drop_index("ix_financial_accounts_connection_id", table_name="financial_accounts")
    op.drop_table("financial_accounts")
    op.drop_index("ix_financial_link_sessions_user_id", table_name="financial_link_sessions")
    op.drop_index(
        "ix_financial_link_sessions_token_hash", table_name="financial_link_sessions"
    )
    op.drop_table("financial_link_sessions")
    op.drop_index("ix_financial_connections_status", table_name="financial_connections")
    op.drop_index("ix_financial_connections_user_id", table_name="financial_connections")
    op.drop_table("financial_connections")
    op.drop_index("ix_scheduled_tasks_idempotency_key", table_name="scheduled_tasks")
    op.drop_index("ix_scheduled_tasks_conversation_status", table_name="scheduled_tasks")
    op.drop_index("ix_scheduled_tasks_status_attempt", table_name="scheduled_tasks")
    op.drop_index("ix_scheduled_tasks_user_id", table_name="scheduled_tasks")
    op.drop_table("scheduled_tasks")
