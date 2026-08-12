from datetime import UTC, datetime, timedelta
from uuid import uuid4

from benji_api.agents.media import model_attachment
from benji_api.integrations.linq.media import linq_attachment_url_expiration
from benji_api.integrations.linq.schemas import LinqInboundAttachment
from benji_api.models.channel import MessageAttachment


def test_expired_or_non_linq_media_url_is_not_exposed_to_the_model() -> None:
    now = datetime(2026, 8, 11, 12, tzinfo=UTC)
    expired = MessageAttachment(
        message_id=uuid4(),
        provider="linq",
        part_index=0,
        mime_type="image/jpeg",
        source_url="https://cdn.linqapp.com/temporary/photo.jpg?token=secret",
        source_url_expires_at=now - timedelta(seconds=1),
    )
    wrong_host = MessageAttachment(
        message_id=uuid4(),
        provider="linq",
        part_index=0,
        mime_type="image/jpeg",
        source_url="https://example.com/photo.jpg",
    )

    assert model_attachment(expired, now=now).url is None
    assert model_attachment(wrong_host, now=now).url is None


def test_unknown_linq_download_url_gets_a_conservative_expiry() -> None:
    observed_at = datetime(2026, 8, 11, 12, tzinfo=UTC)
    attachment = LinqInboundAttachment(
        part_index=0,
        provider_attachment_id="attachment-1",
        mime_type="image/png",
        url="https://cdn.linqapp.com/temporary/photo.png?signature=secret",
        raw_payload={},
    )

    assert linq_attachment_url_expiration(
        attachment,
        observed_at=observed_at,
    ) == observed_at + timedelta(minutes=10)
