import pytest

from benji_api.services.waitlist_rate_limit import (
    WaitlistRateLimiter,
    WaitlistRateLimitExceeded,
)


@pytest.mark.anyio
async def test_waitlist_limiter_caps_ip_and_recovers_after_window() -> None:
    now = [100.0]
    limiter = WaitlistRateLimiter(
        ip_per_minute=2,
        ip_per_hour=20,
        email_per_hour=20,
        clock=lambda: now[0],
    )

    await limiter.check(client_address="192.0.2.1", normalized_email="one@example.com")
    await limiter.check(client_address="192.0.2.1", normalized_email="two@example.com")
    with pytest.raises(WaitlistRateLimitExceeded) as captured:
        await limiter.check(
            client_address="192.0.2.1",
            normalized_email="three@example.com",
        )
    assert captured.value.retry_after_seconds == 60

    now[0] += 61
    await limiter.check(client_address="192.0.2.1", normalized_email="three@example.com")


@pytest.mark.anyio
async def test_waitlist_limiter_caps_email_across_addresses() -> None:
    limiter = WaitlistRateLimiter(
        ip_per_minute=20,
        ip_per_hour=20,
        email_per_hour=2,
    )

    await limiter.check(client_address="192.0.2.1", normalized_email="same@example.com")
    await limiter.check(client_address="192.0.2.2", normalized_email="same@example.com")
    with pytest.raises(WaitlistRateLimitExceeded):
        await limiter.check(client_address="192.0.2.3", normalized_email="same@example.com")
