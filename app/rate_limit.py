"""Per-client token bucket rate limiting (in memory, reset on restart)."""

import threading
import time
from collections import OrderedDict
from collections.abc import Callable

from starlette.requests import Request


class TokenBucket:
    def __init__(self, capacity: float, refill_per_second: float, now: float) -> None:
        self.capacity = capacity
        self.refill_per_second = refill_per_second
        self.tokens = capacity
        self.updated = now

    def take(self, now: float) -> float:
        """Spend one token. Returns 0 if allowed, else seconds until a token is available."""
        elapsed = max(0.0, now - self.updated)
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_per_second)
        self.updated = now
        if self.tokens >= 1:
            self.tokens -= 1
            return 0.0
        return (1 - self.tokens) / self.refill_per_second


class RateLimiter:
    """One bucket per client. `per_minute` is both the burst size and the refill rate."""

    def __init__(
        self,
        per_minute: float,
        clock: Callable[[], float] = time.monotonic,
        max_clients: int = 10_000,
    ) -> None:
        self.capacity = per_minute
        self.refill_per_second = per_minute / 60
        self.max_clients = max_clients
        self._clock = clock
        self._buckets: OrderedDict[str, TokenBucket] = OrderedDict()
        self._lock = threading.Lock()

    def check(self, client: str) -> float:
        """Returns 0 if the request may proceed, else the wait in seconds."""
        with self._lock:
            now = self._clock()
            bucket = self._buckets.get(client)
            if bucket is None:
                bucket = self._buckets[client] = TokenBucket(
                    self.capacity, self.refill_per_second, now
                )
                if len(self._buckets) > self.max_clients:  # forget the least recent client
                    self._buckets.popitem(last=False)
            self._buckets.move_to_end(client)
            return bucket.take(now)


def client_ip(request: Request) -> str:
    """First X-Forwarded-For hop when behind a proxy (e.g. Render), else the socket peer.

    The header is client-controlled, so this is only trustworthy behind a proxy that sets it.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    return request.client.host if request.client else "unknown"
