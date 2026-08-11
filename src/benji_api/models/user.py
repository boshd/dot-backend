from datetime import UTC, date, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import Date, DateTime, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from benji_api.db.base import Base


def utc_now() -> datetime:
    return datetime.now(UTC)


class OnboardingStatus(StrEnum):
    COLLECTING_PROFILE = "collecting_profile"
    COMPLETE = "complete"


class OnboardingStep(StrEnum):
    NAME = "name"
    BIRTH_DATE = "birth_date"
    LOCATION = "location"
    COMPLETE = "complete"


class LanguagePreference(StrEnum):
    AUTO = "auto"
    ENGLISH = "english"
    ARABIC_SCRIPT = "arabic_script"
    EGYPTIAN_FRANCO = "egyptian_franco"


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        Index("ix_users_phone_number", "phone_number", unique=True),
        Index("ix_users_onboarding_status", "onboarding_status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    phone_number: Mapped[str] = mapped_column(String(32), nullable=False)
    phone_verified_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )

    display_name: Mapped[str | None] = mapped_column(String(120))
    birth_date: Mapped[date | None] = mapped_column(Date)
    location_text: Mapped[str | None] = mapped_column(String(255))
    location_city: Mapped[str | None] = mapped_column(String(120))
    location_country: Mapped[str | None] = mapped_column(String(120))

    onboarding_status: Mapped[str] = mapped_column(
        String(32), default=OnboardingStatus.COLLECTING_PROFILE.value, nullable=False
    )
    onboarding_step: Mapped[str] = mapped_column(
        String(32), default=OnboardingStep.NAME.value, nullable=False
    )
    profile_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    messaging_opted_out_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    preferred_language_mode: Mapped[str] = mapped_column(
        String(32),
        default=LanguagePreference.AUTO.value,
        server_default=LanguagePreference.AUTO.value,
        nullable=False,
    )
    language_preference_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )

    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )
