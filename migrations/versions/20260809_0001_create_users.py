"""Create users.

Revision ID: 20260809_0001
Revises:
Create Date: 2026-08-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260809_0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("phone_number", sa.String(length=32), nullable=False),
        sa.Column("phone_verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=True),
        sa.Column("birth_date", sa.Date(), nullable=True),
        sa.Column("location_text", sa.String(length=255), nullable=True),
        sa.Column("onboarding_status", sa.String(length=32), nullable=False),
        sa.Column("onboarding_step", sa.String(length=32), nullable=False),
        sa.Column("profile_completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_users_onboarding_status", "users", ["onboarding_status"])
    op.create_index("ix_users_phone_number", "users", ["phone_number"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_users_phone_number", table_name="users")
    op.drop_index("ix_users_onboarding_status", table_name="users")
    op.drop_table("users")

