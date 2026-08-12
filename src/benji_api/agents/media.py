from datetime import UTC, datetime
from urllib.parse import urlsplit

from benji_api.agents.types import AgentAttachment
from benji_api.models.channel import MessageAttachment

MODEL_IMAGE_MIME_TYPES = {
    "image/jpeg",
    "image/png",
    "image/webp",
}
MODEL_FILE_MIME_TYPES = {"application/pdf"}
MAX_MODEL_ATTACHMENT_BYTES = 20 * 1024 * 1024


def model_attachment(
    attachment: MessageAttachment,
    *,
    now: datetime | None = None,
) -> AgentAttachment:
    mime_type = attachment.mime_type.casefold() if attachment.mime_type else None
    if mime_type in MODEL_IMAGE_MIME_TYPES:
        kind = "image"
    elif mime_type in MODEL_FILE_MIME_TYPES:
        kind = "file"
    else:
        kind = "media"

    return AgentAttachment(
        kind=kind,
        mime_type=mime_type,
        filename=attachment.filename,
        url=(
            attachment.source_url
            if kind != "media" and _url_is_model_safe(attachment, now=now)
            else None
        ),
        provider=attachment.provider,
        provider_id=attachment.provider_attachment_id,
        size_bytes=attachment.size_bytes,
    )


def _url_is_model_safe(
    attachment: MessageAttachment,
    *,
    now: datetime | None,
) -> bool:
    if attachment.provider != "linq" or attachment.source_url is None:
        return False
    if attachment.size_bytes is not None and attachment.size_bytes > MAX_MODEL_ATTACHMENT_BYTES:
        return False
    parsed = urlsplit(attachment.source_url)
    try:
        port = parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme.casefold() != "https"
        or parsed.hostname is None
        or parsed.hostname.casefold() != "cdn.linqapp.com"
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.fragment
    ):
        return False
    expires_at = attachment.source_url_expires_at
    if expires_at is None:
        return True
    current = now or datetime.now(UTC)
    normalized_expiration = (
        expires_at.replace(tzinfo=UTC) if expires_at.tzinfo is None else expires_at.astimezone(UTC)
    )
    return normalized_expiration > current.astimezone(UTC)
