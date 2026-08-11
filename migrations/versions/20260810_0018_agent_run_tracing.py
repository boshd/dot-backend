"""Add replay metadata to agent runs.

Revision ID: 20260810_0018
Revises: 20260810_0017
Create Date: 2026-08-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260810_0018"
down_revision: str | None = "20260810_0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("agent_runs", sa.Column("prompt_version", sa.String(64), nullable=True))
    op.add_column("agent_runs", sa.Column("prompt_hash", sa.String(64), nullable=True))
    op.add_column("agent_runs", sa.Column("prompt_snapshot", sa.JSON(), nullable=True))
    op.add_column("agent_runs", sa.Column("retrieved_memory", sa.JSON(), nullable=True))
    op.add_column("agent_runs", sa.Column("exposed_tools", sa.JSON(), nullable=True))
    op.add_column("agent_runs", sa.Column("reasoning_effort", sa.String(32), nullable=True))
    op.add_column("agent_runs", sa.Column("raw_output", sa.JSON(), nullable=True))
    op.add_column("agent_runs", sa.Column("token_usage", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_runs", "token_usage")
    op.drop_column("agent_runs", "raw_output")
    op.drop_column("agent_runs", "reasoning_effort")
    op.drop_column("agent_runs", "exposed_tools")
    op.drop_column("agent_runs", "retrieved_memory")
    op.drop_column("agent_runs", "prompt_snapshot")
    op.drop_column("agent_runs", "prompt_hash")
    op.drop_column("agent_runs", "prompt_version")
