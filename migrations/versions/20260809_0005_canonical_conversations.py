"""Unify direct messages into canonical cross-channel conversations.

Revision ID: 20260809_0005
Revises: 20260809_0004
Create Date: 2026-08-09
"""

from collections import defaultdict
from collections.abc import Sequence
from uuid import uuid4

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import RowMapping

revision: str = "20260809_0005"
down_revision: str | None = "20260809_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.rename_table("channel_conversations", "conversations")
    op.rename_table("channel_messages", "messages")

    op.add_column(
        "conversations",
        sa.Column("kind", sa.String(length=32), nullable=False, server_default="direct"),
    )
    op.create_table(
        "conversation_channels",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=False),
        sa.Column("service", sa.String(length=32), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "provider", "external_id", name="uq_conversation_channels_provider_id"
        ),
    )
    op.create_index(
        "ix_conversation_channels_conversation_id",
        "conversation_channels",
        ["conversation_id"],
    )
    op.add_column("messages", sa.Column("source_binding_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_messages_source_binding_id",
        "messages",
        "conversation_channels",
        ["source_binding_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_table(
        "message_deliveries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("channel_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("raw_payload", sa.JSON(), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["channel_id"], ["conversation_channels.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "provider", "external_id", name="uq_message_deliveries_provider_id"
        ),
        sa.UniqueConstraint(
            "provider",
            "idempotency_key",
            name="uq_message_deliveries_provider_idempotency",
        ),
    )
    op.create_index(
        "ix_message_deliveries_message_id", "message_deliveries", ["message_id"]
    )
    op.create_index(
        "ix_message_deliveries_channel_id", "message_deliveries", ["channel_id"]
    )

    connection = op.get_bind()
    conversations = sa.table(
        "conversations",
        sa.column("id", sa.Uuid()),
        sa.column("user_id", sa.Uuid()),
        sa.column("provider", sa.String()),
        sa.column("external_id", sa.String()),
        sa.column("service", sa.String()),
        sa.column("status", sa.String()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    channels = sa.table(
        "conversation_channels",
        sa.column("id", sa.Uuid()),
        sa.column("conversation_id", sa.Uuid()),
        sa.column("provider", sa.String()),
        sa.column("external_id", sa.String()),
        sa.column("service", sa.String()),
        sa.column("status", sa.String()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    messages = sa.table(
        "messages",
        sa.column("id", sa.Uuid()),
        sa.column("conversation_id", sa.Uuid()),
        sa.column("provider", sa.String()),
        sa.column("external_id", sa.String()),
        sa.column("idempotency_key", sa.String()),
        sa.column("direction", sa.String()),
        sa.column("status", sa.String()),
        sa.column("raw_payload", sa.JSON()),
        sa.column("sent_at", sa.DateTime(timezone=True)),
        sa.column("delivered_at", sa.DateTime(timezone=True)),
        sa.column("read_at", sa.DateTime(timezone=True)),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
        sa.column("source_binding_id", sa.Uuid()),
    )
    deliveries = sa.table(
        "message_deliveries",
        sa.column("id", sa.Uuid()),
        sa.column("message_id", sa.Uuid()),
        sa.column("channel_id", sa.Uuid()),
        sa.column("provider", sa.String()),
        sa.column("external_id", sa.String()),
        sa.column("idempotency_key", sa.String()),
        sa.column("status", sa.String()),
        sa.column("raw_payload", sa.JSON()),
        sa.column("sent_at", sa.DateTime(timezone=True)),
        sa.column("delivered_at", sa.DateTime(timezone=True)),
        sa.column("read_at", sa.DateTime(timezone=True)),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    agent_runs = sa.table(
        "agent_runs",
        sa.column("conversation_id", sa.Uuid()),
    )

    conversation_rows = list(connection.execute(sa.select(conversations)).mappings())
    rows_by_user: dict[object, list[RowMapping]] = defaultdict(list)
    for row in conversation_rows:
        rows_by_user[row["user_id"]].append(row)

    canonical_by_conversation: dict[object, object] = {}
    channel_by_conversation: dict[object, object] = {}
    channel_rows = []
    for user_rows in rows_by_user.values():
        ordered = sorted(user_rows, key=lambda row: (row["created_at"], str(row["id"])))
        canonical_id = ordered[0]["id"]
        latest_updated_at = max(row["updated_at"] for row in ordered)
        connection.execute(
            sa.update(conversations)
            .where(conversations.c.id == canonical_id)
            .values(updated_at=latest_updated_at)
        )
        for row in ordered:
            channel_id = uuid4()
            canonical_by_conversation[row["id"]] = canonical_id
            channel_by_conversation[row["id"]] = channel_id
            channel_rows.append(
                {
                    "id": channel_id,
                    "conversation_id": row["id"],
                    "provider": row["provider"],
                    "external_id": row["external_id"],
                    "service": row["service"],
                    "status": row["status"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                }
            )
    if channel_rows:
        connection.execute(sa.insert(channels), channel_rows)

    message_rows = list(connection.execute(sa.select(messages)).mappings())
    delivery_rows = []
    allowed_delivery_statuses = {"pending", "sent", "delivered", "read", "failed"}
    for row in message_rows:
        old_conversation_id = row["conversation_id"]
        channel_id = channel_by_conversation[old_conversation_id]
        connection.execute(
            sa.update(messages)
            .where(messages.c.id == row["id"])
            .values(
                conversation_id=canonical_by_conversation[old_conversation_id],
                source_binding_id=channel_id,
            )
        )
        if row["direction"] == "outbound":
            status = row["status"]
            if status not in allowed_delivery_statuses:
                status = "pending"
            delivery_rows.append(
                {
                    "id": uuid4(),
                    "message_id": row["id"],
                    "channel_id": channel_id,
                    "provider": row["provider"],
                    "external_id": row["external_id"],
                    "idempotency_key": row["idempotency_key"],
                    "status": status,
                    "raw_payload": row["raw_payload"],
                    "sent_at": row["sent_at"],
                    "delivered_at": row["delivered_at"],
                    "read_at": row["read_at"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                }
            )
            connection.execute(
                sa.update(messages)
                .where(messages.c.id == row["id"])
                .values(external_id=None, status="completed")
            )
        else:
            connection.execute(
                sa.update(messages)
                .where(messages.c.id == row["id"])
                .values(status="received")
            )
    if delivery_rows:
        connection.execute(sa.insert(deliveries), delivery_rows)

    for old_id, canonical_id in canonical_by_conversation.items():
        if old_id == canonical_id:
            continue
        connection.execute(
            sa.update(agent_runs)
            .where(agent_runs.c.conversation_id == old_id)
            .values(conversation_id=canonical_id)
        )
        connection.execute(
            sa.update(channels)
            .where(channels.c.conversation_id == old_id)
            .values(conversation_id=canonical_id)
        )
        connection.execute(sa.delete(conversations).where(conversations.c.id == old_id))

    connection.execute(sa.update(conversations).values(status="active"))

    op.drop_constraint(
        "uq_channel_conversations_provider_id", "conversations", type_="unique"
    )
    op.drop_index("ix_channel_conversations_user_id", table_name="conversations")
    op.drop_column("conversations", "provider")
    op.drop_column("conversations", "external_id")
    op.drop_column("conversations", "service")
    op.create_index("ix_conversations_user_id", "conversations", ["user_id"])
    op.create_index(
        "uq_conversations_direct_user",
        "conversations",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("kind = 'direct'"),
        sqlite_where=sa.text("kind = 'direct'"),
    )
    op.alter_column("conversations", "kind", server_default=None)

    op.drop_constraint("uq_channel_messages_provider_id", "messages", type_="unique")
    op.drop_constraint(
        "uq_channel_messages_provider_idempotency", "messages", type_="unique"
    )
    op.drop_index("ix_channel_messages_conversation_id", table_name="messages")
    op.drop_index("ix_channel_messages_user_id", table_name="messages")
    op.alter_column("messages", "provider", new_column_name="source_channel")
    op.alter_column("messages", "external_id", new_column_name="source_external_id")
    op.drop_column("messages", "sent_at")
    op.drop_column("messages", "delivered_at")
    op.drop_column("messages", "read_at")
    op.create_index("ix_messages_conversation_id", "messages", ["conversation_id"])
    op.create_index("ix_messages_user_id", "messages", ["user_id"])
    op.create_index("ix_messages_source_binding_id", "messages", ["source_binding_id"])
    op.create_unique_constraint(
        "uq_messages_source_external_id",
        "messages",
        ["source_channel", "source_external_id"],
    )
    op.create_unique_constraint(
        "uq_messages_source_idempotency",
        "messages",
        ["source_channel", "idempotency_key"],
    )


def downgrade() -> None:
    connection = op.get_bind()
    conversations = sa.table(
        "conversations",
        sa.column("id", sa.Uuid()),
        sa.column("provider", sa.String()),
        sa.column("external_id", sa.String()),
        sa.column("service", sa.String()),
    )
    channels = sa.table(
        "conversation_channels",
        sa.column("id", sa.Uuid()),
        sa.column("conversation_id", sa.Uuid()),
        sa.column("provider", sa.String()),
        sa.column("external_id", sa.String()),
        sa.column("service", sa.String()),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    messages = sa.table(
        "messages",
        sa.column("id", sa.Uuid()),
        sa.column("direction", sa.String()),
        sa.column("status", sa.String()),
        sa.column("source_external_id", sa.String()),
        sa.column("source_binding_id", sa.Uuid()),
        sa.column("sent_at", sa.DateTime(timezone=True)),
        sa.column("delivered_at", sa.DateTime(timezone=True)),
        sa.column("read_at", sa.DateTime(timezone=True)),
    )
    deliveries = sa.table(
        "message_deliveries",
        sa.column("message_id", sa.Uuid()),
        sa.column("channel_id", sa.Uuid()),
        sa.column("external_id", sa.String()),
        sa.column("status", sa.String()),
        sa.column("sent_at", sa.DateTime(timezone=True)),
        sa.column("delivered_at", sa.DateTime(timezone=True)),
        sa.column("read_at", sa.DateTime(timezone=True)),
    )

    op.add_column("conversations", sa.Column("provider", sa.String(length=32)))
    op.add_column("conversations", sa.Column("external_id", sa.String(length=255)))
    op.add_column("conversations", sa.Column("service", sa.String(length=32)))
    op.add_column("messages", sa.Column("sent_at", sa.DateTime(timezone=True)))
    op.add_column("messages", sa.Column("delivered_at", sa.DateTime(timezone=True)))
    op.add_column("messages", sa.Column("read_at", sa.DateTime(timezone=True)))

    channel_rows = list(connection.execute(sa.select(channels)).mappings())
    by_conversation: dict[object, list[RowMapping]] = defaultdict(list)
    for row in channel_rows:
        by_conversation[row["conversation_id"]].append(row)
    for conversation_id, rows in by_conversation.items():
        preferred = min(
            rows,
            key=lambda row: (
                0 if row["provider"] == "linq" else 1,
                row["created_at"],
                str(row["id"]),
            ),
        )
        connection.execute(
            sa.update(conversations)
            .where(conversations.c.id == conversation_id)
            .values(
                provider=preferred["provider"],
                external_id=preferred["external_id"],
                service=preferred["service"],
            )
        )

    delivery_by_message = {
        row["message_id"]: row
        for row in connection.execute(sa.select(deliveries)).mappings()
    }
    for row in connection.execute(sa.select(messages)).mappings():
        delivery = delivery_by_message.get(row["id"])
        values: dict[str, object] = {
            "status": "received" if row["direction"] == "inbound" else "sent"
        }
        if delivery is not None:
            values.update(
                source_external_id=delivery["external_id"],
                status=delivery["status"],
                sent_at=delivery["sent_at"],
                delivered_at=delivery["delivered_at"],
                read_at=delivery["read_at"],
            )
        connection.execute(
            sa.update(messages).where(messages.c.id == row["id"]).values(**values)
        )

    op.drop_constraint("uq_messages_source_idempotency", "messages", type_="unique")
    op.drop_constraint("uq_messages_source_external_id", "messages", type_="unique")
    op.drop_index("ix_messages_source_binding_id", table_name="messages")
    op.drop_index("ix_messages_user_id", table_name="messages")
    op.drop_index("ix_messages_conversation_id", table_name="messages")
    op.drop_constraint("fk_messages_source_binding_id", "messages", type_="foreignkey")
    op.drop_column("messages", "source_binding_id")
    op.alter_column("messages", "source_channel", new_column_name="provider")
    op.alter_column("messages", "source_external_id", new_column_name="external_id")
    op.create_index(
        "ix_channel_messages_conversation_id", "messages", ["conversation_id"]
    )
    op.create_index("ix_channel_messages_user_id", "messages", ["user_id"])
    op.create_unique_constraint(
        "uq_channel_messages_provider_id", "messages", ["provider", "external_id"]
    )
    op.create_unique_constraint(
        "uq_channel_messages_provider_idempotency",
        "messages",
        ["provider", "idempotency_key"],
    )

    op.drop_index("uq_conversations_direct_user", table_name="conversations")
    op.drop_index("ix_conversations_user_id", table_name="conversations")
    op.alter_column("conversations", "provider", nullable=False)
    op.alter_column("conversations", "external_id", nullable=False)
    op.create_index(
        "ix_channel_conversations_user_id", "conversations", ["user_id"]
    )
    op.create_unique_constraint(
        "uq_channel_conversations_provider_id",
        "conversations",
        ["provider", "external_id"],
    )
    op.drop_column("conversations", "kind")

    op.drop_index("ix_message_deliveries_channel_id", table_name="message_deliveries")
    op.drop_index("ix_message_deliveries_message_id", table_name="message_deliveries")
    op.drop_table("message_deliveries")
    op.drop_index(
        "ix_conversation_channels_conversation_id", table_name="conversation_channels"
    )
    op.drop_table("conversation_channels")
    op.rename_table("messages", "channel_messages")
    op.rename_table("conversations", "channel_conversations")
