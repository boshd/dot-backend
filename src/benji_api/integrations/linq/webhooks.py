import base64
import hashlib
import hmac
import time
from collections.abc import Mapping


class LinqWebhookVerificationError(ValueError):
    pass


def verify_linq_webhook(
    *,
    secret: str,
    body: bytes,
    headers: Mapping[str, str],
    tolerance_seconds: int = 300,
    now: int | None = None,
) -> None:
    webhook_id = headers.get("webhook-id")
    timestamp_text = headers.get("webhook-timestamp")
    signatures = headers.get("webhook-signature")
    if not webhook_id or not timestamp_text or not signatures:
        raise LinqWebhookVerificationError("required Standard Webhooks headers are missing")

    try:
        timestamp = int(timestamp_text)
    except ValueError as error:
        raise LinqWebhookVerificationError("webhook timestamp is invalid") from error

    current_time = int(time.time()) if now is None else now
    if abs(current_time - timestamp) > tolerance_seconds:
        raise LinqWebhookVerificationError("webhook timestamp is outside the replay window")

    encoded_secret = secret.removeprefix("whsec_")
    try:
        key = base64.b64decode(encoded_secret, validate=True)
    except (ValueError, base64.binascii.Error) as error:
        raise LinqWebhookVerificationError("webhook secret is invalid") from error

    signed_content = b".".join((webhook_id.encode(), timestamp_text.encode(), body))
    expected = base64.b64encode(hmac.new(key, signed_content, hashlib.sha256).digest()).decode()

    candidates = (signature[3:] for signature in signatures.split() if signature.startswith("v1,"))
    if not any(hmac.compare_digest(expected, candidate) for candidate in candidates):
        raise LinqWebhookVerificationError("webhook signature is invalid")
