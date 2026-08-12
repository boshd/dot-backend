from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from benji_api.db.base import Base
from benji_api.models.user import utc_now


class WaitlistStatus(StrEnum):
    WAITING = "waiting"
    INVITED = "invited"
    CONVERTED = "converted"


class WaitlistEntry(Base):
    __tablename__ = "waitlist_entries"
    __table_args__ = (
        Index("ix_waitlist_entries_normalized_email", "normalized_email", unique=True),
        Index("ix_waitlist_entries_referral_code", "referral_code", unique=True),
        Index("ix_waitlist_entries_referred_by_id", "referred_by_id"),
        Index("ix_waitlist_entries_status", "status"),
        Index("ix_waitlist_entries_created_at", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    normalized_email: Mapped[str] = mapped_column(String(320), nullable=False)
    referral_code: Mapped[str] = mapped_column(String(32), nullable=False)
    referred_by_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("waitlist_entries.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(
        String(24),
        default=WaitlistStatus.WAITING.value,
        server_default=WaitlistStatus.WAITING.value,
        nullable=False,
    )
    source: Mapped[str | None] = mapped_column(String(64))
    utm_source: Mapped[str | None] = mapped_column(String(120))
    utm_medium: Mapped[str | None] = mapped_column(String(120))
    utm_campaign: Mapped[str | None] = mapped_column(String(200))
    utm_term: Mapped[str | None] = mapped_column(String(200))
    utm_content: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )
