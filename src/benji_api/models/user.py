from datetime import UTC, date, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
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


class UserIdentifierKind(StrEnum):
    PHONE = "phone"
    EMAIL = "email"


class UserIdentifierStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        Index("ix_users_phone_number", "phone_number", unique=True),
        Index("ix_users_onboarding_status", "onboarding_status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    # Compatibility cache for the user's primary phone. New identity resolution uses
    # UserIdentifier so email-only users do not need a synthetic phone number.
    phone_number: Mapped[str | None] = mapped_column(String(32))
    phone_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

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
    language_preference_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

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


class UserIdentifier(Base):
    __tablename__ = "user_identifiers"
    __table_args__ = (
        UniqueConstraint(
            "kind",
            "normalized_value",
            name="uq_user_identifiers_kind_value",
        ),
        Index("ix_user_identifiers_user_id", "user_id"),
        Index("ix_user_identifiers_status", "status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    normalized_value: Mapped[str] = mapped_column(String(320), nullable=False)
    display_value: Mapped[str | None] = mapped_column(String(320))
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), default=UserIdentifierStatus.ACTIVE.value, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )
