import asyncio
import math
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from time import monotonic


@dataclass(frozen=True, slots=True)
class RateLimitRule:
    namespace: str
    limit: int
    window_seconds: float


class AuthRateLimitExceeded(RuntimeError):
    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__("Too many authentication attempts")
        self.retry_after_seconds = max(1, retry_after_seconds)


class AuthEligibilityRateLimiter:
    """Small single-process limiter for the public auth discovery endpoint.

    Railway runs one API replica for the MVP. A shared Redis/edge limiter should replace this
    before increasing the replica count, but this still prevents cheap enumeration and accidental
    SMS abuse on the current deployment.
    """

    def __init__(
        self,
        *,
        ip_per_minute: int,
        ip_per_hour: int,
        identifier_per_hour: int,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._rules = (
            RateLimitRule("ip-minute", max(1, ip_per_minute), 60),
            RateLimitRule("ip-hour", max(1, ip_per_hour), 3_600),
            RateLimitRule("identifier-hour", max(1, identifier_per_hour), 3_600),
        )
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()
        self._clock = clock

    async def check(self, *, client_address: str, normalized_identifier: str) -> None:
        now = self._clock()
        client_key = _opaque_key(client_address)
        identifier_key = _opaque_key(normalized_identifier)
        scoped_keys = (
            f"{self._rules[0].namespace}:{client_key}",
            f"{self._rules[1].namespace}:{client_key}",
            f"{self._rules[2].namespace}:{identifier_key}",
        )
        async with self._lock:
            for rule, key in zip(self._rules, scoped_keys, strict=True):
                events = self._events[key]
                cutoff = now - rule.window_seconds
                while events and events[0] <= cutoff:
                    events.popleft()
                if len(events) >= rule.limit:
                    retry_after = math.ceil(rule.window_seconds - (now - events[0]))
                    raise AuthRateLimitExceeded(retry_after)
            for key in scoped_keys:
                self._events[key].append(now)


def _opaque_key(value: str) -> str:
    return sha256(value.strip().casefold().encode()).hexdigest()
