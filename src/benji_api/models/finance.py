from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from benji_api.db.base import Base
from benji_api.models.user import utc_now


class FinancialConnectionStatus(StrEnum):
    ACTIVE = "active"
    NEEDS_REAUTHORIZATION = "needs_reauthorization"
    REVOKED = "revoked"


class FinancialSyncStatus(StrEnum):
    PENDING = "pending"
    SYNCING = "syncing"
    IDLE = "idle"
    FAILED = "failed"


class FinancialGoalStatus(StrEnum):
    ACTIVE = "active"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class FinancialConnection(Base):
    __tablename__ = "financial_connections"
    __table_args__ = (
        UniqueConstraint(
            "provider", "provider_connection_id", name="uq_financial_connection_provider_id"
        ),
        Index("ix_financial_connections_user_id", "user_id"),
        Index("ix_financial_connections_status", "status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_connection_id: Mapped[str] = mapped_column(String(255), nullable=False)
    institution_id: Mapped[str | None] = mapped_column(String(255))
    institution_name: Mapped[str] = mapped_column(String(255), nullable=False)
    credentials_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), default=FinancialConnectionStatus.ACTIVE.value, nullable=False
    )
    sync_status: Mapped[str] = mapped_column(
        String(24), default=FinancialSyncStatus.PENDING.value, nullable=False
    )
    sync_cursor: Mapped[str | None] = mapped_column(Text)
    consent_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_sync_error: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class FinancialLinkSession(Base):
    __tablename__ = "financial_link_sessions"
    __table_args__ = (
        Index("ix_financial_link_sessions_token_hash", "exchange_token_hash", unique=True),
        Index("ix_financial_link_sessions_user_id", "user_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    connection_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("financial_connections.id", ondelete="CASCADE")
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    exchange_token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    initiated_channel: Mapped[str] = mapped_column(String(32), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class FinancialAccount(Base):
    __tablename__ = "financial_accounts"
    __table_args__ = (
        UniqueConstraint(
            "connection_id", "provider_account_id", name="uq_financial_account_connection_id"
        ),
        Index("ix_financial_accounts_connection_id", "connection_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    connection_id: Mapped[UUID] = mapped_column(
        ForeignKey("financial_connections.id", ondelete="CASCADE"), nullable=False
    )
    provider_account_id: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    official_name: Mapped[str | None] = mapped_column(String(255))
    mask: Mapped[str | None] = mapped_column(String(16))
    account_type: Mapped[str] = mapped_column(String(64), nullable=False)
    account_subtype: Mapped[str | None] = mapped_column(String(64))
    currency: Mapped[str | None] = mapped_column(String(16))
    current_balance: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    available_balance: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    hidden: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class FinancialTransaction(Base):
    __tablename__ = "financial_transactions"
    __table_args__ = (
        UniqueConstraint(
            "source", "provider_transaction_id", name="uq_financial_transaction_source_id"
        ),
        Index("ix_financial_transactions_account_date", "account_id", "transaction_date"),
        Index("ix_financial_transactions_user_date", "user_id", "transaction_date"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    account_id: Mapped[UUID] = mapped_column(
        ForeignKey("financial_accounts.id", ondelete="CASCADE"), nullable=False
    )
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_transaction_id: Mapped[str] = mapped_column(String(255), nullable=False)
    pending_transaction_id: Mapped[str | None] = mapped_column(String(255))
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False)
    currency: Mapped[str | None] = mapped_column(String(16))
    transaction_date: Mapped[date] = mapped_column(Date, nullable=False)
    authorized_date: Mapped[date | None] = mapped_column(Date)
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    merchant_name: Mapped[str | None] = mapped_column(String(255))
    pending: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    category_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class FinancialGoal(Base):
    __tablename__ = "financial_goals"
    __table_args__ = (
        Index("ix_financial_goals_user_status", "user_id", "status"),
        Index("ix_financial_goals_schedule_id", "schedule_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    schedule_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("scheduled_tasks.id", ondelete="SET NULL")
    )
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    target_amount: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False)
    currency: Mapped[str] = mapped_column(String(16), nullable=False)
    target_date: Mapped[date] = mapped_column(Date, nullable=False)
    baseline_amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    status: Mapped[str] = mapped_column(
        String(24), default=FinancialGoalStatus.ACTIVE.value, nullable=False
    )
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )
