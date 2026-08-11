"""Add durable generated apps and records.

Revision ID: 20260810_0012
Revises: 20260810_0011
Create Date: 2026-08-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260810_0012"
down_revision: str | None = "20260810_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "generated_apps",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("public_id", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=120), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=False),
        sa.Column("template", sa.String(length=64), nullable=False),
        sa.Column("theme", sa.String(length=32), nullable=False),
        sa.Column("access_mode", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("current_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_generated_apps_conversation_id", "generated_apps", ["conversation_id"]
    )
    op.create_index("ix_generated_apps_public_id", "generated_apps", ["public_id"], unique=True)
    op.create_index("ix_generated_apps_user_id", "generated_apps", ["user_id"])

    op.create_table(
        "generated_app_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("app_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("specification", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["app_id"], ["generated_apps.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("app_id", "version", name="uq_generated_app_versions_app_version"),
    )
    op.create_index(
        "ix_generated_app_versions_app_id", "generated_app_versions", ["app_id"]
    )

    op.create_table(
        "generated_app_records",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("app_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("actor_name", sa.String(length=120), nullable=True),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["app_id"], ["generated_apps.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_generated_app_records_app_id", "generated_app_records", ["app_id"]
    )
    op.create_index(
        "ix_generated_app_records_app_kind",
        "generated_app_records",
        ["app_id", "kind"],
    )


def downgrade() -> None:
    op.drop_index("ix_generated_app_records_app_kind", table_name="generated_app_records")
    op.drop_index("ix_generated_app_records_app_id", table_name="generated_app_records")
    op.drop_table("generated_app_records")
    op.drop_index("ix_generated_app_versions_app_id", table_name="generated_app_versions")
    op.drop_table("generated_app_versions")
    op.drop_index("ix_generated_apps_user_id", table_name="generated_apps")
    op.drop_index("ix_generated_apps_public_id", table_name="generated_apps")
    op.drop_index("ix_generated_apps_conversation_id", table_name="generated_apps")
    op.drop_table("generated_apps")
