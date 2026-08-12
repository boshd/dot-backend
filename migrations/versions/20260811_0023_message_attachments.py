"""Persist inbound message attachment metadata.

Revision ID: 20260811_0023
Revises: 20260811_0022
Create Date: 2026-08-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260811_0023"
down_revision: str | None = "20260811_0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "message_attachments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("provider_attachment_id", sa.String(length=255), nullable=True),
        sa.Column("part_index", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=32), server_default="media", nullable=False),
        sa.Column("filename", sa.String(length=512), nullable=True),
        sa.Column("mime_type", sa.String(length=255), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("source_url_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw_payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("message_id", "part_index", name="uq_message_attachments_part"),
    )
    op.create_index(
        "ix_message_attachments_message_id",
        "message_attachments",
        ["message_id"],
        unique=False,
    )
    op.create_index(
        "ix_message_attachments_provider_id",
        "message_attachments",
        ["provider", "provider_attachment_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_message_attachments_provider_id", table_name="message_attachments")
    op.drop_index("ix_message_attachments_message_id", table_name="message_attachments")
    op.drop_table("message_attachments")
