"""Journal agent tool calls and bind generated-app builds to their source call.

Revision ID: 20260812_0027
Revises: 20260812_0026
Create Date: 2026-08-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260812_0027"
down_revision: str | None = "20260812_0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent_tool_calls",
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "agent_tool_calls",
        sa.Column("claimed_by", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "agent_tool_calls",
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.alter_column("agent_tool_calls", "attempts", server_default=None)
    op.create_unique_constraint(
        "uq_agent_tool_calls_run_external_call",
        "agent_tool_calls",
        ["agent_run_id", "external_call_id"],
    )
    op.add_column(
        "generated_app_build_jobs",
        sa.Column("idempotency_key", sa.String(length=160), nullable=True),
    )
    op.add_column(
        "generated_app_build_jobs",
        sa.Column("request_hash", sa.String(length=64), nullable=True),
    )
    op.create_unique_constraint(
        "uq_generated_app_build_jobs_idempotency",
        "generated_app_build_jobs",
        ["idempotency_key"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_generated_app_build_jobs_idempotency",
        "generated_app_build_jobs",
        type_="unique",
    )
    op.drop_column("generated_app_build_jobs", "request_hash")
    op.drop_column("generated_app_build_jobs", "idempotency_key")
    op.drop_constraint(
        "uq_agent_tool_calls_run_external_call",
        "agent_tool_calls",
        type_="unique",
    )
    op.drop_column("agent_tool_calls", "lease_expires_at")
    op.drop_column("agent_tool_calls", "claimed_by")
    op.drop_column("agent_tool_calls", "attempts")
