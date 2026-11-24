import threading
import time

import pytest

from app.cache import TTLCache


def test_miss_then_hit(clock) -> None:
    cache = TTLCache(10, 60, clock)
    assert cache.get_or_load("k", lambda: 1) == (1, False)
    assert cache.get_or_load("k", lambda: 2) == (1, True)
    assert cache.stats()["hits"] == 1
    assert cache.stats()["misses"] == 1
    assert cache.stats()["hit_rate"] == 0.5


def test_entries_expire_after_ttl(clock) -> None:
    cache = TTLCache(10, 60, clock)
    cache.get_or_load("k", lambda: "old")
    clock.advance(59)
    assert cache.get_or_load("k", lambda: "new") == ("old", True)
    clock.advance(1)
    assert cache.get_or_load("k", lambda: "new") == ("new", False)


def test_lru_eviction_keeps_recently_used(clock) -> None:
    cache = TTLCache(2, 60, clock)
    cache.get_or_load("a", lambda: "a")
    cache.get_or_load("b", lambda: "b")
    cache.get_or_load("a", lambda: "a")      # touch a, so b is least recent
    cache.get_or_load("c", lambda: "c")      # evicts b
    assert cache.stats()["size"] == 2
    assert cache.stats()["evictions"] == 1
    assert cache.get_or_load("a", lambda: "x")[1] is True
    assert cache.get_or_load("b", lambda: "b2") == ("b2", False)


def test_zero_ttl_disables_caching(clock) -> None:
    cache = TTLCache(10, 0, clock)
    cache.get_or_load("k", lambda: 1)
    assert cache.get_or_load("k", lambda: 2) == (2, False)
    assert cache.stats()["size"] == 0


def test_loader_errors_are_not_cached(clock) -> None:
    cache = TTLCache(10, 60, clock)

    def boom() -> None:
        raise RuntimeError("db down")

    with pytest.raises(RuntimeError):
        cache.get_or_load("k", boom)
    assert cache.get_or_load("k", lambda: "ok") == ("ok", False)


def test_concurrent_misses_load_once() -> None:
    cache = TTLCache(10, 60)
    calls = 0
    start = threading.Barrier(20)

    def slow_loader() -> str:
        nonlocal calls
        calls += 1
        time.sleep(0.1)
        return "value"

    results = []

    def worker() -> None:
        start.wait()
        results.append(cache.get_or_load("cold", slow_loader))

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert calls == 1
    assert all(v == "value" for v, _ in results)
    assert sum(1 for _, hit in results if not hit) == 1
