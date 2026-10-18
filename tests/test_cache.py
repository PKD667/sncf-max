"""Unit tests for the process-wide cache and the request budget.

No network, no sleeping: both take an injectable clock, which is the point of
the design and the reason these can be tested at all.
"""

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from network.cache import TTLCache, TokenBucket


class Clock:
    """A monotonic clock that only moves when told to."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


# -- TTLCache ---------------------------------------------------------------

def test_get_or_computes_once_then_reuses():
    clock = Clock()
    cache = TTLCache(ttl=10, clock=clock)
    calls = []

    assert cache.get_or("k", lambda: calls.append(1) or "v") == "v"
    assert cache.get_or("k", lambda: calls.append(2) or "w") == "v"
    assert calls == [1]
    assert cache.stats()["hits"] == 1


def test_entries_expire_after_ttl():
    clock = Clock()
    cache = TTLCache(ttl=10, clock=clock)
    cache.put("k", "v")
    clock.advance(9.9)
    assert cache.get("k") == "v"
    clock.advance(0.2)
    assert cache.get("k") is None
    assert cache.stats()["entries"] == 0


def test_oldest_entry_is_evicted_first():
    clock = Clock()
    cache = TTLCache(ttl=100, maxsize=2, clock=clock)
    cache.put("a", 1)
    cache.put("b", 2)
    cache.get("a")            # "a" is now the most recently used
    cache.put("c", 3)
    assert cache.get("a") == 1
    assert cache.get("b") is None
    assert cache.get("c") == 3


def test_single_flight_collapses_concurrent_misses():
    cache = TTLCache(ttl=100, clock=Clock())
    calls = []
    started = threading.Barrier(8)

    def compute():
        calls.append(threading.get_ident())
        time.sleep(0.05)          # a real fetch takes time; others must wait
        return "value"

    def worker(results):
        started.wait()
        results.append(cache.get_or("same", compute))

    results = []
    threads = [threading.Thread(target=worker, args=(results,)) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results == ["value"] * 8
    assert len(calls) == 1, f"computed {len(calls)} times, want 1"


def test_a_failing_fill_is_not_cached():
    cache = TTLCache(ttl=100, clock=Clock())
    boom = RuntimeError("upstream down")

    def fail():
        raise boom

    with pytest.raises(RuntimeError):
        cache.get_or("k", fail)
    assert cache.get("k") is None
    assert cache.get_or("k", lambda: "recovered") == "recovered"


def test_a_failing_fill_does_not_wedge_waiters():
    cache = TTLCache(ttl=100, clock=Clock())
    started = threading.Event()
    gate = threading.Event()
    outcome = []

    def record(fn):
        try:
            outcome.append(fn())
        except RuntimeError:
            outcome.append("raised")

    def owner():
        started.set()
        gate.wait(5)
        raise RuntimeError("nope")

    owner_thread = threading.Thread(target=record, args=(lambda: cache.get_or("k", owner),))
    owner_thread.start()
    started.wait(5)

    waiters = [threading.Thread(target=record,
                                args=(lambda: cache.get_or("k", lambda: "second"),))
               for _ in range(3)]
    for t in waiters:
        t.start()
    gate.set()                      # the owner fails; waiters must recover
    for t in waiters + [owner_thread]:
        t.join(timeout=5)

    assert outcome.count("raised") == 1
    assert outcome.count("second") == 3
    assert cache.get("k") == "second"


# -- TokenBucket ------------------------------------------------------------

def test_burst_requests_do_not_wait():
    clock = Clock()
    sleeps = []
    bucket = TokenBucket(rate=10, burst=4, clock=clock, sleep=sleeps.append)

    waits = [bucket.take() for _ in range(4)]
    assert waits == [0.0, 0.0, 0.0, 0.0]
    assert sleeps == []


def test_rate_paces_requests_past_the_burst():
    clock = Clock()
    bucket = TokenBucket(rate=10, burst=1, clock=clock, sleep=lambda s: None)
    # The bucket does not advance the clock itself (the sleep is stubbed), so
    # every take after the first reserves one interval further out.
    waits = [bucket.take() for _ in range(5)]
    assert waits[0] == 0.0
    for earlier, later in zip(waits, waits[1:]):
        assert later == pytest.approx(earlier + 0.1, abs=1e-6)


def test_waiting_is_what_rate_limit_does():
    """A token bucket at an infinite rate must never sleep."""
    bucket = TokenBucket(rate=1_000_000, burst=1000)
    start = time.monotonic()
    for _ in range(50):
        bucket.take()
    assert time.monotonic() - start < 0.1
