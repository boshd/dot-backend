"""Rename legacy default group titles from Benji to Dot.

Revision ID: 20260810_0015
Revises: 20260810_0014
Create Date: 2026-08-10
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260810_0015"
down_revision: str | None = "20260810_0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "UPDATE conversations SET title = 'group with dot' "
        "WHERE kind = 'group' AND title = 'group with benji'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE conversations SET title = 'group with benji' "
        "WHERE kind = 'group' AND title = 'group with dot'"
    )
