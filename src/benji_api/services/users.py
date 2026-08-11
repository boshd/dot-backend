from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from benji_api.models.user import User
from benji_api.schemas.phone import normalize_phone_number


@dataclass(frozen=True, slots=True)
class UserResolution:
    user: User
    created: bool


async def resolve_user_from_phone(
    session: AsyncSession,
    phone_number: str,
) -> UserResolution:
    """Return the user for an inbound phone number, creating one exactly once."""
    normalized_phone = normalize_phone_number(phone_number)
    now = datetime.now(UTC)
    user = await session.scalar(select(User).where(User.phone_number == normalized_phone))

    if user is not None:
        user.last_seen_at = now
        return UserResolution(user=user, created=False)

    user = User(
        phone_number=normalized_phone,
        phone_verified_at=now,
        first_seen_at=now,
        last_seen_at=now,
    )

    try:
        async with session.begin_nested():
            session.add(user)
            await session.flush()
    except IntegrityError:
        # Another inbound message may have created the same phone identity first.
        user = await session.scalar(select(User).where(User.phone_number == normalized_phone))
        if user is None:
            raise
        user.last_seen_at = now
        return UserResolution(user=user, created=False)

    return UserResolution(user=user, created=True)
