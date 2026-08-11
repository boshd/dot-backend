from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from benji_api.db.base import Base
from benji_api.models.user import utc_now


class IntegrationStatus(StrEnum):
    ACTIVE = "active"
    NEEDS_REAUTHORIZATION = "needs_reauthorization"
    REVOKED = "revoked"


class IntegrationSubscriptionStatus(StrEnum):
    ACTIVE = "active"
    PENDING_CONFIGURATION = "pending_configuration"
    EXPIRED = "expired"
    STOPPED = "stopped"
    FAILED = "failed"


class IntegrationAccount(Base):
    __tablename__ = "integration_accounts"
    __table_args__ = (
        UniqueConstraint(
            "provider",
            "provider_account_id",
            name="uq_integration_accounts_provider_account",
        ),
        Index("ix_integration_accounts_user_id", "user_id"),
        Index("ix_integration_accounts_provider_email", "provider", "email"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_account_id: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(255))
    credentials_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    granted_scopes: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), default=IntegrationStatus.ACTIVE.value, nullable=False
    )
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_connected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class IntegrationGrant(Base):
    __tablename__ = "integration_grants"
    __table_args__ = (
        UniqueConstraint("account_id", "integration_key", name="uq_integration_grants_account_key"),
        Index("ix_integration_grants_account_id", "account_id"),
        Index("ix_integration_grants_integration_key", "integration_key"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    account_id: Mapped[UUID] = mapped_column(
        ForeignKey("integration_accounts.id", ondelete="CASCADE"), nullable=False
    )
    integration_key: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), default=IntegrationStatus.ACTIVE.value, nullable=False
    )
    connected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class IntegrationOAuthState(Base):
    __tablename__ = "integration_oauth_states"
    __table_args__ = (
        Index("ix_integration_oauth_states_token_hash", "token_hash", unique=True),
        Index("ix_integration_oauth_states_user_id", "user_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    integration_key: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    requested_scopes: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    initiated_channel: Mapped[str] = mapped_column(String(32), nullable=False)
    redirect_after: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class IntegrationConnectLink(Base):
    __tablename__ = "integration_connect_links"
    __table_args__ = (
        Index("ix_integration_connect_links_token_hash", "token_hash", unique=True),
        Index("ix_integration_connect_links_user_id", "user_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    integration_key: Mapped[str] = mapped_column(String(64), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class IntegrationSubscription(Base):
    __tablename__ = "integration_subscriptions"
    __table_args__ = (
        UniqueConstraint(
            "provider",
            "provider_subscription_id",
            name="uq_integration_subscriptions_provider_id",
        ),
        Index("ix_integration_subscriptions_account_id", "account_id"),
        Index("ix_integration_subscriptions_integration_key", "integration_key"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    account_id: Mapped[UUID] = mapped_column(
        ForeignKey("integration_accounts.id", ondelete="CASCADE"), nullable=False
    )
    integration_key: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_subscription_id: Mapped[str] = mapped_column(String(255), nullable=False)
    provider_resource_id: Mapped[str | None] = mapped_column(String(255))
    verification_token_hash: Mapped[str | None] = mapped_column(String(64))
    cursor: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(
        String(32),
        default=IntegrationSubscriptionStatus.PENDING_CONFIGURATION.value,
        nullable=False,
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_notification_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )
