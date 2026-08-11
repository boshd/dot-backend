"""Add group conversation membership, sender attribution, and invites.

Revision ID: 20260810_0013
Revises: 20260810_0012
Create Date: 2026-08-10
"""

from collections.abc import Sequence
from uuid import uuid4

import sqlalchemy as sa
from alembic import op

revision: str = "20260810_0013"
down_revision: str | None = "20260810_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("conversations", sa.Column("title", sa.String(length=120)))
    op.add_column("conversations", sa.Column("avatar_url", sa.String(length=2048)))
    op.add_column(
        "conversations",
        sa.Column(
            "response_mode",
            sa.String(length=32),
            nullable=False,
            server_default="mentions",
        ),
    )

    op.create_table(
        "conversation_members",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid()),
        sa.Column("external_handle", sa.String(length=255), nullable=False),
        sa.Column("external_id", sa.String(length=255)),
        sa.Column("service", sa.String(length=32)),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("left_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["conversations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "conversation_id", "external_handle", name="uq_conversation_members_handle"
        ),
        sa.UniqueConstraint(
            "conversation_id", "user_id", name="uq_conversation_members_user"
        ),
    )
    op.create_index(
        "ix_conversation_members_conversation_id",
        "conversation_members",
        ["conversation_id"],
    )
    op.create_index(
        "ix_conversation_members_user_id", "conversation_members", ["user_id"]
    )

    op.create_table(
        "conversation_invites",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("max_uses", sa.Integer(), nullable=False),
        sa.Column("use_count", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["conversations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"], ["users.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_conversation_invites_conversation_id",
        "conversation_invites",
        ["conversation_id"],
    )
    op.create_index(
        "ix_conversation_invites_token_hash",
        "conversation_invites",
        ["token_hash"],
        unique=True,
    )

    op.add_column("messages", sa.Column("sender_user_id", sa.Uuid()))
    op.create_foreign_key(
        "fk_messages_sender_user_id",
        "messages",
        "users",
        ["sender_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_messages_sender_user_id", "messages", ["sender_user_id"])

    connection = op.get_bind()
    conversations = sa.table(
        "conversations",
        sa.column("id", sa.Uuid()),
        sa.column("user_id", sa.Uuid()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    users = sa.table(
        "users", sa.column("id", sa.Uuid()), sa.column("phone_number", sa.String())
    )
    members = sa.table(
        "conversation_members",
        sa.column("id", sa.Uuid()),
        sa.column("conversation_id", sa.Uuid()),
        sa.column("user_id", sa.Uuid()),
        sa.column("external_handle", sa.String()),
        sa.column("role", sa.String()),
        sa.column("status", sa.String()),
        sa.column("joined_at", sa.DateTime(timezone=True)),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    rows = connection.execute(
        sa.select(
            conversations.c.id,
            conversations.c.user_id,
            conversations.c.created_at,
            conversations.c.updated_at,
            users.c.phone_number,
        ).join(users, users.c.id == conversations.c.user_id)
    ).mappings()
    member_rows = [
        {
            "id": uuid4(),
            "conversation_id": row["id"],
            "user_id": row["user_id"],
            "external_handle": row["phone_number"],
            "role": "owner",
            "status": "active",
            "joined_at": row["created_at"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        for row in rows
    ]
    if member_rows:
        connection.execute(sa.insert(members), member_rows)
    op.execute(
        "UPDATE messages SET sender_user_id = user_id WHERE direction = 'inbound'"
    )


def downgrade() -> None:
    op.drop_index("ix_messages_sender_user_id", table_name="messages")
    op.drop_constraint("fk_messages_sender_user_id", "messages", type_="foreignkey")
    op.drop_column("messages", "sender_user_id")
    op.drop_index("ix_conversation_invites_token_hash", table_name="conversation_invites")
    op.drop_index(
        "ix_conversation_invites_conversation_id", table_name="conversation_invites"
    )
    op.drop_table("conversation_invites")
    op.drop_index("ix_conversation_members_user_id", table_name="conversation_members")
    op.drop_index(
        "ix_conversation_members_conversation_id", table_name="conversation_members"
    )
    op.drop_table("conversation_members")
    op.drop_column("conversations", "response_mode")
    op.drop_column("conversations", "avatar_url")
    op.drop_column("conversations", "title")
