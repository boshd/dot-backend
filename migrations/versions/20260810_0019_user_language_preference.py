"""Add user language preference.

Revision ID: 20260810_0019
Revises: 20260810_0018
Create Date: 2026-08-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260810_0019"
down_revision: str | None = "20260810_0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "preferred_language_mode",
            sa.String(length=32),
            server_default="auto",
            nullable=False,
        ),
    )
    op.add_column(
        "users",
        sa.Column("language_preference_updated_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "language_preference_updated_at")
    op.drop_column("users", "preferred_language_mode")
