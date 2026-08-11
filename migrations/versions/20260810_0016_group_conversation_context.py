"""Add richer group identity and automatic participation context.

Revision ID: 20260810_0016
Revises: 20260810_0015
Create Date: 2026-08-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260810_0016"
down_revision: str | None = "20260810_0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "conversations", sa.Column("group_owner_source", sa.String(length=32))
    )
    op.add_column(
        "conversation_members", sa.Column("display_name", sa.String(length=120))
    )
    op.execute(
        "UPDATE conversations SET group_owner_source = 'explicit' "
        "WHERE kind = 'group' AND EXISTS ("
        "SELECT 1 FROM conversation_channels "
        "WHERE conversation_channels.conversation_id = conversations.id "
        "AND conversation_channels.provider = 'web')"
    )
    op.execute(
        "UPDATE conversations SET group_owner_source = 'unclaimed', "
        "response_mode = 'auto' WHERE kind = 'group' AND EXISTS ("
        "SELECT 1 FROM conversation_channels "
        "WHERE conversation_channels.conversation_id = conversations.id "
        "AND conversation_channels.provider = 'linq')"
    )
    op.execute(
        "UPDATE conversation_members SET role = 'member' WHERE conversation_id IN ("
        "SELECT conversation_id FROM conversation_channels WHERE provider = 'linq')"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE conversations SET response_mode = 'mentions' "
        "WHERE kind = 'group' AND response_mode = 'auto'"
    )
    op.drop_column("conversation_members", "display_name")
    op.drop_column("conversations", "group_owner_source")
