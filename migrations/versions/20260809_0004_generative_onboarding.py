"""Add structured profile location and agent purpose.

Revision ID: 20260809_0004
Revises: 20260809_0003
Create Date: 2026-08-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260809_0004"
down_revision: str | None = "20260809_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("location_city", sa.String(length=120)))
    op.add_column("users", sa.Column("location_country", sa.String(length=120)))
    op.add_column(
        "agent_runs",
        sa.Column(
            "purpose",
            sa.String(length=32),
            nullable=False,
            server_default="conversation",
        ),
    )


def downgrade() -> None:
    op.drop_column("agent_runs", "purpose")
    op.drop_column("users", "location_country")
    op.drop_column("users", "location_city")
