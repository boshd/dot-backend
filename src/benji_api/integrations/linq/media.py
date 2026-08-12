from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

from benji_api.integrations.linq.schemas import LinqInboundAttachment

_CONSERVATIVE_SIGNED_URL_TTL = timedelta(minutes=10)


def linq_attachment_url_expiration(
    attachment: LinqInboundAttachment,
    *,
    observed_at: datetime,
) -> datetime | None:
    """Record a safe usable window without assuming every Linq CDN URL is permanent."""
    explicit = _explicit_expiration(attachment.raw_payload)
    if explicit is not None:
        return explicit
    if attachment.url is None:
        return None

    parsed = urlsplit(attachment.url)
    query = parse_qs(parsed.query)
    query_expiration = _query_expiration(query)
    if query_expiration is not None:
        return query_expiration

    # Linq documents the partner-scoped path as its persistent tier. Any signed or
    # unfamiliar layout gets a conservative window; the durable attachment ID is
    # retained so a future resolver can obtain a fresh provider URL.
    if not parsed.query and parsed.path.startswith("/attachments/partners/"):
        return None
    return _as_utc(observed_at) + _CONSERVATIVE_SIGNED_URL_TTL


def _explicit_expiration(payload: dict[str, object]) -> datetime | None:
    for key in ("url_expires_at", "download_url_expires_at", "expires_at"):
        parsed = _parse_datetime(payload.get(key))
        if parsed is not None:
            return parsed
    return None


def _query_expiration(query: dict[str, list[str]]) -> datetime | None:
    folded = {key.casefold(): values for key, values in query.items()}
    for key in ("expires", "expires_at"):
        values = folded.get(key)
        if values:
            parsed = _parse_datetime(values[0])
            if parsed is not None:
                return parsed

    dates = folded.get("x-amz-date")
    durations = folded.get("x-amz-expires")
    if dates and durations:
        try:
            issued_at = datetime.strptime(dates[0], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
            return issued_at + timedelta(seconds=max(0, int(durations[0])))
        except (ValueError, OverflowError):
            return None
    return None


def _parse_datetime(value: object) -> datetime | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1_000
        try:
            return datetime.fromtimestamp(timestamp, tz=UTC)
        except (ValueError, OverflowError, OSError):
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip()
    try:
        return _as_utc(datetime.fromisoformat(normalized.replace("Z", "+00:00")))
    except ValueError:
        try:
            timestamp = float(normalized)
            if timestamp > 10_000_000_000:
                timestamp /= 1_000
            return datetime.fromtimestamp(timestamp, tz=UTC)
        except (ValueError, OverflowError, OSError):
            return None


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
