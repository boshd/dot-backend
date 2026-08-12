from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from benji_api.db.base import Base
from benji_api.models.user import utc_now


class GeneratedAppRuntimeKind(StrEnum):
    LEGACY = "legacy"
    CODE = "code"


class GeneratedAppBuildStatus(StrEnum):
    QUEUED = "queued"
    CLAIMED = "claimed"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class GeneratedAppRevisionStatus(StrEnum):
    READY = "ready"
    REJECTED = "rejected"


class GeneratedAppRole(StrEnum):
    OWNER = "owner"
    EDITOR = "editor"
    MEMBER = "member"
    VIEWER = "viewer"


class GeneratedAppRevision(Base):
    """An immutable, tested code-app revision."""

    __tablename__ = "generated_app_revisions"
    __table_args__ = (
        UniqueConstraint("app_id", "revision_number", name="uq_app_revisions_app_number"),
        Index("ix_generated_app_revisions_app_id", "app_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    app_id: Mapped[UUID] = mapped_column(
        ForeignKey("generated_apps.id", ondelete="CASCADE"), nullable=False
    )
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), default=GeneratedAppRevisionStatus.READY.value, nullable=False
    )
    manifest: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    seed_data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    seed_applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_files: Mapped[dict[str, str]] = mapped_column(JSON, default=dict, nullable=False)
    artifact: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    artifact_url: Mapped[str] = mapped_column(Text, nullable=False)
    artifact_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    sdk_version: Mapped[str] = mapped_column(String(64), nullable=False)
    dependency_lock: Mapped[dict[str, str]] = mapped_column(JSON, default=dict, nullable=False)
    test_results: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class GeneratedAppDeployment(Base):
    """The atomic active-revision pointer for a code app."""

    __tablename__ = "generated_app_deployments"
    __table_args__ = (Index("ix_generated_app_deployments_revision", "active_revision_id"),)

    app_id: Mapped[UUID] = mapped_column(
        ForeignKey("generated_apps.id", ondelete="CASCADE"), primary_key=True
    )
    active_revision_id: Mapped[UUID] = mapped_column(
        ForeignKey("generated_app_revisions.id", ondelete="RESTRICT"), nullable=False
    )
    previous_revision_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("generated_app_revisions.id", ondelete="SET NULL")
    )
    deployment_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    deployed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class GeneratedAppBuildJob(Base):
    """Durable queue item consumed by the generated-app builder."""

    __tablename__ = "generated_app_build_jobs"
    __table_args__ = (
        Index("ix_generated_app_build_jobs_app", "app_id", "created_at"),
        Index("ix_generated_app_build_jobs_claim", "status", "lease_expires_at", "created_at"),
        Index(
            "uq_generated_app_build_jobs_live_app",
            "app_id",
            unique=True,
            postgresql_where=text("status IN ('queued', 'claimed')"),
            sqlite_where=text("status IN ('queued', 'claimed')"),
        ),
        UniqueConstraint(
            "idempotency_key",
            name="uq_generated_app_build_jobs_idempotency",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    app_id: Mapped[UUID] = mapped_column(
        ForeignKey("generated_apps.id", ondelete="CASCADE"), nullable=False
    )
    base_revision_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("generated_app_revisions.id", ondelete="SET NULL")
    )
    result_revision_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("generated_app_revisions.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(
        String(24), default=GeneratedAppBuildStatus.QUEUED.value, nullable=False
    )
    request: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(160))
    request_hash: Mapped[str | None] = mapped_column(String(64))
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    delivery_provider: Mapped[str | None] = mapped_column(String(32))
    app_url: Mapped[str] = mapped_column(Text, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    claimed_by: Mapped[str | None] = mapped_column(String(120))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class GeneratedAppMembership(Base):
    __tablename__ = "generated_app_memberships"
    __table_args__ = (
        UniqueConstraint("app_id", "user_id", name="uq_generated_app_memberships_app_user"),
        Index("ix_generated_app_memberships_user", "user_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    app_id: Mapped[UUID] = mapped_column(
        ForeignKey("generated_apps.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(24), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class GeneratedAppDataRecord(Base):
    __tablename__ = "generated_app_data_records"
    __table_args__ = (
        Index("ix_generated_app_data_records_app_entity", "app_id", "entity", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    app_id: Mapped[UUID] = mapped_column(
        ForeignKey("generated_apps.id", ondelete="CASCADE"), nullable=False
    )
    entity: Mapped[str] = mapped_column(String(64), nullable=False)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    data_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    updated_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class GeneratedAppEvent(Base):
    """Append-only audit and automation event emitted with app-data mutations."""

    __tablename__ = "generated_app_events"
    __table_args__ = (
        UniqueConstraint("app_id", "idempotency_key", name="uq_app_events_app_idempotency"),
        Index("ix_generated_app_events_app_created", "app_id", "created_at"),
        Index("ix_generated_app_events_type", "event_type", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    app_id: Mapped[UUID] = mapped_column(
        ForeignKey("generated_apps.id", ondelete="CASCADE"), nullable=False
    )
    record_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("generated_app_data_records.id", ondelete="SET NULL")
    )
    entity: Mapped[str | None] = mapped_column(String(64))
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    actor_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    actor_session_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("generated_app_sessions.id", ondelete="SET NULL")
    )
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    operation: Mapped[str | None] = mapped_column(String(64))
    request_hash: Mapped[str | None] = mapped_column(String(64))
    response: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class GeneratedAppAccessTicket(Base):
    __tablename__ = "generated_app_access_tickets"
    __table_args__ = (Index("ix_generated_app_access_tickets_hash", "token_hash", unique=True),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    app_id: Mapped[UUID] = mapped_column(
        ForeignKey("generated_apps.id", ondelete="CASCADE"), nullable=False
    )
    issued_by_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    principal_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE")
    )
    role: Mapped[str] = mapped_column(String(24), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_redeemed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    redemption_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class GeneratedAppSession(Base):
    __tablename__ = "generated_app_sessions"
    __table_args__ = (
        Index("ix_generated_app_sessions_hash", "token_hash", unique=True),
        Index("ix_generated_app_sessions_app", "app_id", "expires_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    app_id: Mapped[UUID] = mapped_column(
        ForeignKey("generated_apps.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE")
    )
    role: Mapped[str] = mapped_column(String(24), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
