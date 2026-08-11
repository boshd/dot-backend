"""Allow durable user events to target group conversations.

Revision ID: 20260810_0014
Revises: 20260810_0013
Create Date: 2026-08-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260810_0014"
down_revision: str | None = "20260810_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("user_events", sa.Column("conversation_id", sa.Uuid()))
    op.create_foreign_key(
        "fk_user_events_conversation_id",
        "user_events",
        "conversations",
        ["conversation_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_user_events_conversation_id",
        "user_events",
        ["conversation_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_user_events_conversation_id", table_name="user_events")
    op.drop_constraint(
        "fk_user_events_conversation_id", "user_events", type_="foreignkey"
    )
    op.drop_column("user_events", "conversation_id")
