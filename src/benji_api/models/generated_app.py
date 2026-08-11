from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from benji_api.db.base import Base
from benji_api.models.user import utc_now


class GeneratedAppStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class GeneratedAppAccessMode(StrEnum):
    PRIVATE_LINK = "private_link"
    COLLABORATIVE_LINK = "collaborative_link"


class GeneratedApp(Base):
    __tablename__ = "generated_apps"
    __table_args__ = (
        Index("ix_generated_apps_user_id", "user_id"),
        Index("ix_generated_apps_conversation_id", "conversation_id"),
        Index("ix_generated_apps_public_id", "public_id", unique=True),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    public_id: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    template: Mapped[str] = mapped_column(String(64), nullable=False)
    theme: Mapped[str] = mapped_column(String(32), nullable=False)
    access_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), default=GeneratedAppStatus.ACTIVE.value, nullable=False
    )
    current_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class GeneratedAppVersion(Base):
    __tablename__ = "generated_app_versions"
    __table_args__ = (
        UniqueConstraint("app_id", "version", name="uq_generated_app_versions_app_version"),
        Index("ix_generated_app_versions_app_id", "app_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    app_id: Mapped[UUID] = mapped_column(
        ForeignKey("generated_apps.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    specification: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class GeneratedAppRecord(Base):
    __tablename__ = "generated_app_records"
    __table_args__ = (
        Index("ix_generated_app_records_app_id", "app_id"),
        Index("ix_generated_app_records_app_kind", "app_id", "kind"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    app_id: Mapped[UUID] = mapped_column(
        ForeignKey("generated_apps.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_name: Mapped[str | None] = mapped_column(String(120))
    data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )
