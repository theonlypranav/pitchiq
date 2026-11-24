"""In-memory TTL + LRU cache with per-key locks.

Concurrent misses for the same key run the loader once: the first caller loads,
the others wait on that key's lock and then read the stored value (a HIT).
"""

import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

_MISSING = object()


class TTLCache:
    def __init__(
        self,
        max_entries: int,
        ttl_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_entries = max_entries
        self.ttl = ttl_seconds
        self._clock = clock
        self._data: OrderedDict[str, tuple[Any, float]] = OrderedDict()  # key -> (value, expires_at)
        self._lock = threading.Lock()  # guards _data, _key_locks and counters
        self._key_locks: dict[str, list] = {}  # key -> [Lock, number of callers using it]
        self.hits = self.misses = self.evictions = 0

    @property
    def enabled(self) -> bool:
        return self.ttl > 0

    def _get_fresh(self, key: str) -> Any:
        entry = self._data.get(key)
        if entry is None:
            return _MISSING
        value, expires_at = entry
        if self._clock() >= expires_at:
            del self._data[key]
            return _MISSING
        self._data.move_to_end(key)
        return value

    def _store(self, key: str, value: Any) -> None:
        self._data[key] = (value, self._clock() + self.ttl)
        self._data.move_to_end(key)
        while len(self._data) > self.max_entries:
            self._data.popitem(last=False)
            self.evictions += 1

    def get_or_load(self, key: str, loader: Callable[[], Any]) -> tuple[Any, bool]:
        """Return (value, was_hit). With ttl <= 0 caching is disabled (always a miss)."""
        if not self.enabled:
            with self._lock:
                self.misses += 1
            return loader(), False

        with self._lock:
            value = self._get_fresh(key)
            if value is not _MISSING:
                self.hits += 1
                return value, True
            slot = self._key_locks.setdefault(key, [threading.Lock(), 0])
            slot[1] += 1
        try:
            with slot[0]:
                with self._lock:
                    value = self._get_fresh(key)  # filled while we waited?
                    if value is not _MISSING:
                        self.hits += 1
                        return value, True
                    self.misses += 1
                value = loader()  # an exception here caches nothing
                with self._lock:
                    self._store(key, value)
                return value, False
        finally:
            with self._lock:
                slot[1] -= 1
                if slot[1] == 0:
                    del self._key_locks[key]

    def stats(self) -> dict[str, Any]:
        with self._lock:
            total = self.hits + self.misses
            return {
                "hits": self.hits,
                "misses": self.misses,
                "size": len(self._data),
                "hit_rate": round(self.hits / total, 4) if total else 0.0,
                "evictions": self.evictions,
                "max_entries": self.max_entries,
                "ttl_seconds": self.ttl,
            }
