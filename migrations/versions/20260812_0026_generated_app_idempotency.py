"""Bind generated app idempotency keys to exact requests and responses.

Revision ID: 20260812_0026
Revises: 20260812_0025
Create Date: 2026-08-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260812_0026"
down_revision: str | None = "20260812_0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "generated_app_events",
        sa.Column("operation", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "generated_app_events",
        sa.Column("request_hash", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "generated_app_events",
        sa.Column("response", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
    )
    op.alter_column("generated_app_events", "response", server_default=None)


def downgrade() -> None:
    op.drop_column("generated_app_events", "response")
    op.drop_column("generated_app_events", "request_hash")
    op.drop_column("generated_app_events", "operation")
