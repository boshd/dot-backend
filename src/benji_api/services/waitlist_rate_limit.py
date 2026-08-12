import asyncio
import math
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from time import monotonic


@dataclass(frozen=True, slots=True)
class WaitlistRateLimitRule:
    namespace: str
    limit: int
    window_seconds: float


class WaitlistRateLimitExceeded(RuntimeError):
    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__("Too many waitlist attempts")
        self.retry_after_seconds = max(1, retry_after_seconds)


class WaitlistRateLimiter:
    """Small single-process limiter for the public waitlist join endpoint."""

    def __init__(
        self,
        *,
        ip_per_minute: int,
        ip_per_hour: int,
        email_per_hour: int,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._rules = (
            WaitlistRateLimitRule("ip-minute", max(1, ip_per_minute), 60),
            WaitlistRateLimitRule("ip-hour", max(1, ip_per_hour), 3_600),
            WaitlistRateLimitRule("email-hour", max(1, email_per_hour), 3_600),
        )
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()
        self._clock = clock

    async def check(self, *, client_address: str, normalized_email: str) -> None:
        now = self._clock()
        client_key = _opaque_key(client_address)
        email_key = _opaque_key(normalized_email)
        scoped_keys = (
            f"{self._rules[0].namespace}:{client_key}",
            f"{self._rules[1].namespace}:{client_key}",
            f"{self._rules[2].namespace}:{email_key}",
        )
        async with self._lock:
            for rule, key in zip(self._rules, scoped_keys, strict=True):
                events = self._events[key]
                cutoff = now - rule.window_seconds
                while events and events[0] <= cutoff:
                    events.popleft()
                if len(events) >= rule.limit:
                    retry_after = math.ceil(rule.window_seconds - (now - events[0]))
                    raise WaitlistRateLimitExceeded(retry_after)
            for key in scoped_keys:
                self._events[key].append(now)


def _opaque_key(value: str) -> str:
    return sha256(value.strip().casefold().encode()).hexdigest()
