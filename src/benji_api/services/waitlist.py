import secrets
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from benji_api.models.waitlist import WaitlistEntry
from benji_api.services.users import normalize_email_address

REFERRAL_CODE_BYTES = 9
REFERRAL_CODE_ATTEMPTS = 5


class WaitlistEntryNotFoundError(LookupError):
    pass


@dataclass(frozen=True, slots=True)
class WaitlistResult:
    entry: WaitlistEntry
    joined: bool
    position: int
    referral_count: int


async def join_waitlist(
    session: AsyncSession,
    *,
    email: str,
    referral_code: str | None = None,
    source: str | None = None,
    utm_source: str | None = None,
    utm_medium: str | None = None,
    utm_campaign: str | None = None,
    utm_term: str | None = None,
    utm_content: str | None = None,
) -> WaitlistResult:
    normalized_email = normalize_email_address(email)
    existing = await _entry_by_email(session, normalized_email=normalized_email)
    if existing is not None:
        return await _result(session, entry=existing, joined=False)

    referrer = await _entry_by_referral_code(session, referral_code=referral_code)
    metadata = {
        "source": _optional_text(source, max_length=64),
        "utm_source": _optional_text(utm_source, max_length=120),
        "utm_medium": _optional_text(utm_medium, max_length=120),
        "utm_campaign": _optional_text(utm_campaign, max_length=200),
        "utm_term": _optional_text(utm_term, max_length=200),
        "utm_content": _optional_text(utm_content, max_length=200),
    }

    for _ in range(REFERRAL_CODE_ATTEMPTS):
        entry = WaitlistEntry(
            normalized_email=normalized_email,
            referral_code=secrets.token_urlsafe(REFERRAL_CODE_BYTES),
            referred_by_id=referrer.id if referrer is not None else None,
            **metadata,
        )
        try:
            async with session.begin_nested():
                session.add(entry)
                await session.flush()
        except IntegrityError:
            existing = await _entry_by_email(session, normalized_email=normalized_email)
            if existing is not None:
                return await _result(session, entry=existing, joined=False)
            continue

        await session.commit()
        return await _result(session, entry=entry, joined=True)

    raise RuntimeError("Could not allocate a unique waitlist referral code")


async def get_waitlist_referral_stats(
    session: AsyncSession,
    *,
    referral_code: str,
) -> WaitlistResult:
    entry = await _entry_by_referral_code(session, referral_code=referral_code)
    if entry is None:
        raise WaitlistEntryNotFoundError("Referral was not found")
    return await _result(session, entry=entry, joined=False)


async def _entry_by_email(
    session: AsyncSession,
    *,
    normalized_email: str,
) -> WaitlistEntry | None:
    return await session.scalar(
        select(WaitlistEntry).where(WaitlistEntry.normalized_email == normalized_email)
    )


async def _entry_by_referral_code(
    session: AsyncSession,
    *,
    referral_code: str | None,
) -> WaitlistEntry | None:
    clean_code = _optional_text(referral_code, max_length=64)
    if clean_code is None:
        return None
    return await session.scalar(
        select(WaitlistEntry).where(WaitlistEntry.referral_code == clean_code)
    )


async def _result(
    session: AsyncSession,
    *,
    entry: WaitlistEntry,
    joined: bool,
) -> WaitlistResult:
    referral_counts = (
        select(
            WaitlistEntry.referred_by_id.label("entry_id"),
            func.count(WaitlistEntry.id).label("referral_count"),
        )
        .where(WaitlistEntry.referred_by_id.is_not(None))
        .group_by(WaitlistEntry.referred_by_id)
        .subquery()
    )
    referral_count = func.coalesce(referral_counts.c.referral_count, 0)
    ranked_entries = (
        select(
            WaitlistEntry.id.label("entry_id"),
            referral_count.label("referral_count"),
            func.row_number()
            .over(
                order_by=(
                    referral_count.desc(),
                    WaitlistEntry.created_at.asc(),
                    WaitlistEntry.id.asc(),
                )
            )
            .label("position"),
        )
        .outerjoin(
            referral_counts,
            referral_counts.c.entry_id == WaitlistEntry.id,
        )
        .subquery()
    )
    stats = (
        await session.execute(
            select(ranked_entries.c.position, ranked_entries.c.referral_count).where(
                ranked_entries.c.entry_id == entry.id
            )
        )
    ).one()
    return WaitlistResult(
        entry=entry,
        joined=joined,
        position=int(stats.position),
        referral_count=int(stats.referral_count),
    )


def _optional_text(value: str | None, *, max_length: int) -> str | None:
    if value is None:
        return None
    clean = " ".join(value.split())
    if not clean:
        return None
    if len(clean) > max_length:
        raise ValueError(f"Value must be at most {max_length} characters")
    return clean
