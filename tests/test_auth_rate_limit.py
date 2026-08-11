import pytest

from benji_api.services.auth_rate_limit import (
    AuthEligibilityRateLimiter,
    AuthRateLimitExceeded,
)


@pytest.mark.anyio
async def test_auth_limiter_caps_ip_and_recovers_after_window() -> None:
    now = [100.0]
    limiter = AuthEligibilityRateLimiter(
        ip_per_minute=2,
        ip_per_hour=20,
        identifier_per_hour=20,
        clock=lambda: now[0],
    )

    await limiter.check(client_address="192.0.2.1", normalized_identifier="one@example.com")
    await limiter.check(client_address="192.0.2.1", normalized_identifier="two@example.com")
    with pytest.raises(AuthRateLimitExceeded) as captured:
        await limiter.check(client_address="192.0.2.1", normalized_identifier="three@example.com")
    assert captured.value.retry_after_seconds == 60

    now[0] += 61
    await limiter.check(client_address="192.0.2.1", normalized_identifier="three@example.com")


@pytest.mark.anyio
async def test_auth_limiter_caps_identifier_across_addresses() -> None:
    limiter = AuthEligibilityRateLimiter(
        ip_per_minute=20,
        ip_per_hour=20,
        identifier_per_hour=2,
    )

    await limiter.check(client_address="192.0.2.1", normalized_identifier="same@example.com")
    await limiter.check(client_address="192.0.2.2", normalized_identifier="same@example.com")
    with pytest.raises(AuthRateLimitExceeded):
        await limiter.check(client_address="192.0.2.3", normalized_identifier="same@example.com")
