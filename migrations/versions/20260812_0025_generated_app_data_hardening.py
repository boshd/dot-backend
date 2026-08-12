"""Harden generated app revision data and shared access quotas.

Revision ID: 20260812_0025
Revises: 20260812_0024
Create Date: 2026-08-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260812_0025"
down_revision: str | None = "20260812_0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    op.add_column(
        "generated_app_revisions",
        sa.Column("title", sa.String(length=120), nullable=True),
    )
    op.add_column(
        "generated_app_revisions",
        sa.Column("description", sa.String(length=500), nullable=True),
    )
    op.add_column(
        "generated_app_revisions",
        sa.Column("seed_data", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
    )
    op.add_column(
        "generated_app_revisions",
        sa.Column("seed_applied_at", sa.DateTime(timezone=True), nullable=True),
    )
    if bind.dialect.name == "postgresql":
        op.execute(
            "UPDATE generated_app_revisions AS revision "
            "SET title = app.title, description = app.description "
            "FROM generated_apps AS app WHERE app.id = revision.app_id"
        )
    else:
        op.execute(
            "UPDATE generated_app_revisions "
            "SET title = (SELECT title FROM generated_apps "
            "WHERE generated_apps.id = generated_app_revisions.app_id), "
            "description = (SELECT description FROM generated_apps "
            "WHERE generated_apps.id = generated_app_revisions.app_id)"
        )
    op.alter_column("generated_app_revisions", "title", nullable=False)
    op.alter_column("generated_app_revisions", "description", nullable=False)
    op.alter_column("generated_app_revisions", "seed_data", server_default=None)

    op.add_column(
        "generated_app_data_records",
        sa.Column("data_bytes", sa.Integer(), server_default="0", nullable=False),
    )
    if bind.dialect.name == "postgresql":
        op.execute(
            "UPDATE generated_app_data_records "
            "SET data_bytes = octet_length(CAST(data AS text))"
        )
    else:
        op.execute(
            "UPDATE generated_app_data_records "
            "SET data_bytes = length(CAST(data AS text))"
        )
    op.alter_column("generated_app_data_records", "data_bytes", server_default=None)


def downgrade() -> None:
    op.drop_column("generated_app_data_records", "data_bytes")
    op.drop_column("generated_app_revisions", "seed_applied_at")
    op.drop_column("generated_app_revisions", "seed_data")
    op.drop_column("generated_app_revisions", "description")
    op.drop_column("generated_app_revisions", "title")
