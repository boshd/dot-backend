"""Add canonical phone and email user identifiers.

Revision ID: 20260811_0020
Revises: 20260810_0019
Create Date: 2026-08-11
"""

from collections.abc import Sequence
from uuid import uuid4

import sqlalchemy as sa
from alembic import op

revision: str = "20260811_0020"
down_revision: str | None = "20260810_0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user_identifiers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("normalized_value", sa.String(length=320), nullable=False),
        sa.Column("display_value", sa.String(length=320)),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True)),
        sa.Column("is_primary", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "kind",
            "normalized_value",
            name="uq_user_identifiers_kind_value",
        ),
    )
    op.create_index(
        "ix_user_identifiers_user_id",
        "user_identifiers",
        ["user_id"],
    )
    op.create_index(
        "ix_user_identifiers_status",
        "user_identifiers",
        ["status"],
    )

    connection = op.get_bind()
    users = sa.table(
        "users",
        sa.column("id", sa.Uuid()),
        sa.column("phone_number", sa.String()),
        sa.column("phone_verified_at", sa.DateTime(timezone=True)),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    identifiers = sa.table(
        "user_identifiers",
        sa.column("id", sa.Uuid()),
        sa.column("user_id", sa.Uuid()),
        sa.column("kind", sa.String()),
        sa.column("normalized_value", sa.String()),
        sa.column("display_value", sa.String()),
        sa.column("source", sa.String()),
        sa.column("verified_at", sa.DateTime(timezone=True)),
        sa.column("is_primary", sa.Boolean()),
        sa.column("status", sa.String()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    user_rows = connection.execute(
        sa.select(
            users.c.id,
            users.c.phone_number,
            users.c.phone_verified_at,
            users.c.created_at,
            users.c.updated_at,
        )
    ).mappings()
    identifier_rows = [
        {
            "id": uuid4(),
            "user_id": row["id"],
            "kind": "phone",
            "normalized_value": row["phone_number"],
            "display_value": row["phone_number"],
            "source": "legacy_phone",
            "verified_at": row["phone_verified_at"],
            "is_primary": True,
            "status": "active",
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        for row in user_rows
        if row["phone_number"] is not None
    ]
    if identifier_rows:
        connection.execute(sa.insert(identifiers), identifier_rows)

    op.alter_column(
        "users",
        "phone_number",
        existing_type=sa.String(length=32),
        nullable=True,
    )
    op.alter_column(
        "users",
        "phone_verified_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=True,
    )
    op.drop_constraint(
        "uq_auth_identities_user_provider",
        "auth_identities",
        type_="unique",
    )


def downgrade() -> None:
    connection = op.get_bind()
    remaining_email_only = connection.scalar(
        sa.text("SELECT COUNT(*) FROM users WHERE phone_number IS NULL")
    )
    if remaining_email_only:
        raise RuntimeError(
            "Cannot downgrade while email-only users exist; link a phone or remove them first"
        )
    duplicate_auth_providers = connection.scalar(
        sa.text(
            "SELECT COUNT(*) FROM ("
            "SELECT 1 FROM auth_identities GROUP BY user_id, provider HAVING COUNT(*) > 1"
            ") AS duplicate_auth_providers"
        )
    )
    if duplicate_auth_providers:
        raise RuntimeError(
            "Cannot downgrade while a user has multiple identities from the same provider"
        )

    op.create_unique_constraint(
        "uq_auth_identities_user_provider",
        "auth_identities",
        ["user_id", "provider"],
    )
    op.alter_column(
        "users",
        "phone_verified_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=False,
    )
    op.alter_column(
        "users",
        "phone_number",
        existing_type=sa.String(length=32),
        nullable=False,
    )
    op.drop_index("ix_user_identifiers_status", table_name="user_identifiers")
    op.drop_index("ix_user_identifiers_user_id", table_name="user_identifiers")
    op.drop_table("user_identifiers")
