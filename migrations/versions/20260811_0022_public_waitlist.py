"""Add the public referral waitlist.

Revision ID: 20260811_0022
Revises: 20260811_0021
Create Date: 2026-08-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260811_0022"
down_revision: str | None = "20260811_0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "waitlist_entries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("normalized_email", sa.String(length=320), nullable=False),
        sa.Column("referral_code", sa.String(length=32), nullable=False),
        sa.Column("referred_by_id", sa.Uuid(), nullable=True),
        sa.Column(
            "status",
            sa.String(length=24),
            server_default="waiting",
            nullable=False,
        ),
        sa.Column("source", sa.String(length=64), nullable=True),
        sa.Column("utm_source", sa.String(length=120), nullable=True),
        sa.Column("utm_medium", sa.String(length=120), nullable=True),
        sa.Column("utm_campaign", sa.String(length=200), nullable=True),
        sa.Column("utm_term", sa.String(length=200), nullable=True),
        sa.Column("utm_content", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["referred_by_id"],
            ["waitlist_entries.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_waitlist_entries_normalized_email",
        "waitlist_entries",
        ["normalized_email"],
        unique=True,
    )
    op.create_index(
        "ix_waitlist_entries_referral_code",
        "waitlist_entries",
        ["referral_code"],
        unique=True,
    )
    op.create_index(
        "ix_waitlist_entries_referred_by_id",
        "waitlist_entries",
        ["referred_by_id"],
    )
    op.create_index(
        "ix_waitlist_entries_status",
        "waitlist_entries",
        ["status"],
    )
    op.create_index(
        "ix_waitlist_entries_created_at",
        "waitlist_entries",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_waitlist_entries_created_at", table_name="waitlist_entries")
    op.drop_index("ix_waitlist_entries_status", table_name="waitlist_entries")
    op.drop_index("ix_waitlist_entries_referred_by_id", table_name="waitlist_entries")
    op.drop_index("ix_waitlist_entries_referral_code", table_name="waitlist_entries")
    op.drop_index("ix_waitlist_entries_normalized_email", table_name="waitlist_entries")
    op.drop_table("waitlist_entries")
