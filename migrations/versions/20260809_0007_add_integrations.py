"""Add provider-neutral integrations and subscription state.

Revision ID: 20260809_0007
Revises: 20260809_0006
Create Date: 2026-08-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260809_0007"
down_revision: str | None = "20260809_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "integration_accounts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("provider_account_id", sa.String(length=255), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=True),
        sa.Column("credentials_ciphertext", sa.Text(), nullable=False),
        sa.Column("granted_scopes", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_connected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "provider",
            "provider_account_id",
            name="uq_integration_accounts_provider_account",
        ),
    )
    op.create_index(
        "ix_integration_accounts_provider_email",
        "integration_accounts",
        ["provider", "email"],
    )
    op.create_index("ix_integration_accounts_user_id", "integration_accounts", ["user_id"])

    op.create_table(
        "integration_connect_links",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("integration_key", sa.String(length=64), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_integration_connect_links_token_hash",
        "integration_connect_links",
        ["token_hash"],
        unique=True,
    )
    op.create_index(
        "ix_integration_connect_links_user_id",
        "integration_connect_links",
        ["user_id"],
    )

    op.create_table(
        "integration_oauth_states",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("integration_key", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("requested_scopes", sa.JSON(), nullable=False),
        sa.Column("initiated_channel", sa.String(length=32), nullable=False),
        sa.Column("redirect_after", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_integration_oauth_states_token_hash",
        "integration_oauth_states",
        ["token_hash"],
        unique=True,
    )
    op.create_index(
        "ix_integration_oauth_states_user_id",
        "integration_oauth_states",
        ["user_id"],
    )

    op.create_table(
        "integration_grants",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("integration_key", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("connected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["account_id"], ["integration_accounts.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "account_id", "integration_key", name="uq_integration_grants_account_key"
        ),
    )
    op.create_index(
        "ix_integration_grants_account_id", "integration_grants", ["account_id"]
    )
    op.create_index(
        "ix_integration_grants_integration_key",
        "integration_grants",
        ["integration_key"],
    )

    op.create_table(
        "integration_subscriptions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("integration_key", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("provider_subscription_id", sa.String(length=255), nullable=False),
        sa.Column("provider_resource_id", sa.String(length=255), nullable=True),
        sa.Column("verification_token_hash", sa.String(length=64), nullable=True),
        sa.Column("cursor", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_notification_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["account_id"], ["integration_accounts.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "provider",
            "provider_subscription_id",
            name="uq_integration_subscriptions_provider_id",
        ),
    )
    op.create_index(
        "ix_integration_subscriptions_account_id",
        "integration_subscriptions",
        ["account_id"],
    )
    op.create_index(
        "ix_integration_subscriptions_integration_key",
        "integration_subscriptions",
        ["integration_key"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_integration_subscriptions_integration_key",
        table_name="integration_subscriptions",
    )
    op.drop_index(
        "ix_integration_subscriptions_account_id",
        table_name="integration_subscriptions",
    )
    op.drop_table("integration_subscriptions")
    op.drop_index("ix_integration_grants_integration_key", table_name="integration_grants")
    op.drop_index("ix_integration_grants_account_id", table_name="integration_grants")
    op.drop_table("integration_grants")
    op.drop_index(
        "ix_integration_oauth_states_user_id", table_name="integration_oauth_states"
    )
    op.drop_index(
        "ix_integration_oauth_states_token_hash", table_name="integration_oauth_states"
    )
    op.drop_table("integration_oauth_states")
    op.drop_index(
        "ix_integration_connect_links_user_id", table_name="integration_connect_links"
    )
    op.drop_index(
        "ix_integration_connect_links_token_hash", table_name="integration_connect_links"
    )
    op.drop_table("integration_connect_links")
    op.drop_index(
        "ix_integration_accounts_user_id", table_name="integration_accounts"
    )
    op.drop_index(
        "ix_integration_accounts_provider_email", table_name="integration_accounts"
    )
    op.drop_table("integration_accounts")
