from app.rate_limit import RateLimiter, TokenBucket


def test_bucket_allows_burst_then_blocks(clock) -> None:
    limiter = RateLimiter(30, clock)
    assert all(limiter.check("1.2.3.4") == 0 for _ in range(30))
    assert limiter.check("1.2.3.4") == 2.0   # 0.5 tokens/s -> 2 s for the next token


def test_bucket_refills_over_time(clock) -> None:
    limiter = RateLimiter(30, clock)
    for _ in range(30):
        limiter.check("ip")
    clock.advance(1)
    assert limiter.check("ip") == 1.0        # half a token so far
    clock.advance(1)
    assert limiter.check("ip") == 0          # now a full token
    clock.advance(600)
    assert all(limiter.check("ip") == 0 for _ in range(30))   # capped at capacity
    assert limiter.check("ip") > 0


def test_clients_have_separate_buckets(clock) -> None:
    limiter = RateLimiter(2, clock)
    assert limiter.check("a") == 0
    assert limiter.check("a") == 0
    assert limiter.check("a") > 0
    assert limiter.check("b") == 0


def test_least_recent_client_is_forgotten(clock) -> None:
    limiter = RateLimiter(1, clock, max_clients=2)
    limiter.check("a")
    limiter.check("b")
    limiter.check("c")                        # evicts a
    assert limiter.check("a") == 0            # fresh bucket again


def test_token_bucket_partial_tokens() -> None:
    bucket = TokenBucket(capacity=1, refill_per_second=0.5, now=0)
    assert bucket.take(0) == 0
    assert bucket.take(1) == 1.0
