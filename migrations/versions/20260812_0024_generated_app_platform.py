"""Add the generated code-app control and data plane.

Revision ID: 20260812_0024
Revises: 20260811_0023
Create Date: 2026-08-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260812_0024"
down_revision: str | None = "20260811_0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "generated_apps",
        sa.Column("runtime_kind", sa.String(length=24), server_default="legacy", nullable=False),
    )
    op.alter_column("generated_apps", "runtime_kind", server_default=None)
    op.create_table(
        "generated_app_revisions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("app_id", sa.Uuid(), nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("source_files", sa.JSON(), nullable=False),
        sa.Column("artifact", sa.JSON(), nullable=False),
        sa.Column("artifact_url", sa.Text(), nullable=False),
        sa.Column("artifact_sha256", sa.String(length=64), nullable=False),
        sa.Column("sdk_version", sa.String(length=64), nullable=False),
        sa.Column("dependency_lock", sa.JSON(), nullable=False),
        sa.Column("test_results", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["app_id"], ["generated_apps.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("app_id", "revision_number", name="uq_app_revisions_app_number"),
    )
    op.create_index(
        "ix_generated_app_revisions_app_id", "generated_app_revisions", ["app_id"]
    )
    op.create_table(
        "generated_app_deployments",
        sa.Column("app_id", sa.Uuid(), nullable=False),
        sa.Column("active_revision_id", sa.Uuid(), nullable=False),
        sa.Column("previous_revision_id", sa.Uuid(), nullable=True),
        sa.Column("deployment_version", sa.Integer(), nullable=False),
        sa.Column("deployed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["app_id"], ["generated_apps.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["active_revision_id"], ["generated_app_revisions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["previous_revision_id"], ["generated_app_revisions.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("app_id"),
    )
    op.create_index(
        "ix_generated_app_deployments_revision",
        "generated_app_deployments",
        ["active_revision_id"],
    )
    op.create_table(
        "generated_app_build_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("app_id", sa.Uuid(), nullable=False),
        sa.Column("base_revision_id", sa.Uuid(), nullable=True),
        sa.Column("result_revision_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("request", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("delivery_provider", sa.String(length=32), nullable=True),
        sa.Column("app_url", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("claimed_by", sa.String(length=120), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["app_id"], ["generated_apps.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["base_revision_id"], ["generated_app_revisions.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["result_revision_id"], ["generated_app_revisions.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_generated_app_build_jobs_app", "generated_app_build_jobs", ["app_id", "created_at"]
    )
    op.create_index(
        "ix_generated_app_build_jobs_claim",
        "generated_app_build_jobs",
        ["status", "lease_expires_at", "created_at"],
    )
    op.create_index(
        "uq_generated_app_build_jobs_live_app",
        "generated_app_build_jobs",
        ["app_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'claimed')"),
        sqlite_where=sa.text("status IN ('queued', 'claimed')"),
    )
    op.create_table(
        "generated_app_memberships",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("app_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(length=24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["app_id"], ["generated_apps.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("app_id", "user_id", name="uq_generated_app_memberships_app_user"),
    )
    op.create_index(
        "ix_generated_app_memberships_user", "generated_app_memberships", ["user_id"]
    )
    op.create_table(
        "generated_app_data_records",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("app_id", sa.Uuid(), nullable=False),
        sa.Column("entity", sa.String(length=64), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("updated_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["app_id"], ["generated_apps.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["updated_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_generated_app_data_records_app_entity",
        "generated_app_data_records",
        ["app_id", "entity", "created_at"],
    )
    op.create_table(
        "generated_app_access_tickets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("app_id", sa.Uuid(), nullable=False),
        sa.Column("issued_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("principal_user_id", sa.Uuid(), nullable=True),
        sa.Column("role", sa.String(length=24), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_redeemed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("redemption_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["app_id"], ["generated_apps.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["issued_by_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["principal_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_generated_app_access_tickets_hash",
        "generated_app_access_tickets",
        ["token_hash"],
        unique=True,
    )
    op.create_table(
        "generated_app_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("app_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("role", sa.String(length=24), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["app_id"], ["generated_apps.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_generated_app_sessions_hash",
        "generated_app_sessions",
        ["token_hash"],
        unique=True,
    )
    op.create_index(
        "ix_generated_app_sessions_app",
        "generated_app_sessions",
        ["app_id", "expires_at"],
    )
    op.create_table(
        "generated_app_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("app_id", sa.Uuid(), nullable=False),
        sa.Column("record_id", sa.Uuid(), nullable=True),
        sa.Column("entity", sa.String(length=64), nullable=True),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("actor_session_id", sa.Uuid(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["app_id"], ["generated_apps.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["record_id"], ["generated_app_data_records.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["actor_session_id"], ["generated_app_sessions.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("app_id", "idempotency_key", name="uq_app_events_app_idempotency"),
    )
    op.create_index(
        "ix_generated_app_events_app_created", "generated_app_events", ["app_id", "created_at"]
    )
    op.create_index(
        "ix_generated_app_events_type", "generated_app_events", ["event_type", "created_at"]
    )


def downgrade() -> None:
    op.drop_table("generated_app_events")
    op.drop_table("generated_app_sessions")
    op.drop_table("generated_app_access_tickets")
    op.drop_table("generated_app_data_records")
    op.drop_table("generated_app_memberships")
    op.drop_table("generated_app_build_jobs")
    op.drop_table("generated_app_deployments")
    op.drop_table("generated_app_revisions")
    op.drop_column("generated_apps", "runtime_kind")
