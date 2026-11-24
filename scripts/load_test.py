"""Tiny load tester: N concurrent workers hammer one URL for a fixed time.

Usage: python scripts/load_test.py URL [--seconds 30] [--workers 20]
Prints requests/second, latency percentiles and the status-code mix.
"""

import argparse
import statistics
import threading
import time
from collections import Counter

import httpx


def run(url: str, seconds: float, workers: int) -> dict:
    latencies: list[float] = []
    codes: Counter = Counter()
    lock = threading.Lock()
    deadline = time.perf_counter() + seconds

    def worker() -> None:
        with httpx.Client(timeout=10) as client:
            while time.perf_counter() < deadline:
                start = time.perf_counter()
                try:
                    code = client.get(url).status_code
                except httpx.HTTPError:
                    code = "error"
                elapsed = (time.perf_counter() - start) * 1000
                with lock:
                    latencies.append(elapsed)
                    codes[code] += 1

    threads = [threading.Thread(target=worker) for _ in range(workers)]
    started = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    duration = time.perf_counter() - started
    q = statistics.quantiles(latencies, n=100)
    return {
        "requests": len(latencies),
        "rps": round(len(latencies) / duration, 1),
        "p50_ms": round(q[49], 2),
        "p95_ms": round(q[94], 2),
        "p99_ms": round(q[98], 2),
        "codes": dict(codes),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("--seconds", type=float, default=30)
    parser.add_argument("--workers", type=int, default=20)
    args = parser.parse_args()
    print(run(args.url, args.seconds, args.workers))


if __name__ == "__main__":
    main()
