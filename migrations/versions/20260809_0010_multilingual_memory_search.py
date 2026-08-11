"""Use language-neutral full-text search for personal memory.

Revision ID: 20260809_0010
Revises: 20260809_0009
Create Date: 2026-08-09
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260809_0010"
down_revision: str | None = "20260809_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("DROP INDEX ix_memory_facts_statement_fts")
    op.execute(
        "CREATE INDEX ix_memory_facts_statement_fts ON memory_facts "
        "USING gin (to_tsvector('simple', statement))"
    )


def downgrade() -> None:
    op.execute("DROP INDEX ix_memory_facts_statement_fts")
    op.execute(
        "CREATE INDEX ix_memory_facts_statement_fts ON memory_facts "
        "USING gin (to_tsvector('english', statement))"
    )
