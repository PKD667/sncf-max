"""Process-wide caches and the shared upstream request budget.

A single click on the frontend used to mean ~220 queries to data.sncf.com:
one per (origin, destination) pair for every transfer candidate, one per
train to rebuild its stop list, one per destination for the hunt.  Measured
at ~1.1 s per call that is 40-220 s per user, which is how the site ended up
answering 504 instead of results.

Two primitives fix that, and they are the whole point of this module:

``TTLCache``
    Ask the API each question once.  The keys are the questions (station-day,
    train-day, raw query), the values are what came back, and a short TTL
    keeps seat availability honest.  ``get_or`` is *single-flight*: if two
    browsers ask for the same day at once, one fetch happens and both wait
    on it, so a cache miss under load costs what one miss costs, not N.

``TokenBucket``
    Ask at a bounded rate.  The old ``REQUEST_DELAY`` sleep was per client
    object and racy: eight worker threads each slept 1 s and then raced, so
    the dataset saw bursts of eight while every caller waited a second.  A
    bucket shared by the process paces every thread together and lets them
    proceed as fast as the budget allows - fewer total requests, so the
    sustained rate can be *lower* than the burst rate was.

Neither one knows anything about trains; they are deliberately dumb.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any, Callable, Dict, Hashable

_MISS = object()


class TTLCache:
    """Bounded, thread-safe, single-flight cache with a time-to-live.

    Entries expire ``ttl`` seconds after they were stored; the oldest entry
    is evicted once ``maxsize`` is reached, so a long-lived server cannot
    grow without bound.  ``clock`` is injectable for tests.
    """

    def __init__(
        self,
        ttl: float = 1800.0,
        maxsize: int = 256,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ttl = ttl
        self.maxsize = maxsize
        self._clock = clock
        self._lock = threading.Lock()
        self._data: "OrderedDict[Hashable, tuple]" = OrderedDict()
        self._inflight: Dict[Hashable, threading.Event] = {}
        self.hits = 0
        self.misses = 0

    # -- internals ------------------------------------------------------

    def _get_locked(self, key: Hashable) -> Any:
        entry = self._data.get(key)
        if entry is None:
            return _MISS
        expires, value = entry
        if expires <= self._clock():
            del self._data[key]
            return _MISS
        self._data.move_to_end(key)
        return value

    def _put_locked(self, key: Hashable, value: Any) -> None:
        self._data[key] = (self._clock() + self.ttl, value)
        self._data.move_to_end(key)
        while len(self._data) > self.maxsize:
            self._data.popitem(last=False)

    # -- API ------------------------------------------------------------

    def get(self, key: Hashable, default: Any = None) -> Any:
        with self._lock:
            value = self._get_locked(key)
        return default if value is _MISS else value

    def put(self, key: Hashable, value: Any) -> None:
        with self._lock:
            self._put_locked(key, value)

    def get_or(self, key: Hashable, fn: Callable[[], Any]) -> Any:
        """Return the cached value for *key*, computing it with ``fn`` once.

        Concurrent callers for the same missing key wait for the first
        caller's ``fn`` instead of running their own.  If that call raises,
        the waiters retry as the new owner (bounded), so one broken fetch
        does not become a stampede.
        """
        for attempt in range(3):
            with self._lock:
                value = self._get_locked(key)
                if value is not _MISS:
                    if attempt == 0:
                        self.hits += 1
                    return value
                event = self._inflight.get(key)
                owner = event is None
                if owner:
                    event = threading.Event()
                    self._inflight[key] = event
            if not owner:
                event.wait()
                continue
            try:
                value = fn()
            except BaseException:
                with self._lock:
                    self._inflight.pop(key, None)
                event.set()
                raise
            with self._lock:
                self.misses += 1
                self._put_locked(key, value)
                self._inflight.pop(key, None)
            event.set()
            return value
        # Only reachable if three owners in a row failed.
        raise RuntimeError(f"cache fill kept failing for {key!r}")

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "entries": len(self._data),
                "hits": self.hits,
                "misses": self.misses,
                "ttl": self.ttl,
            }


class TokenBucket:
    """Rate limiter shared by every caller in the process.

    Generic cell rate algorithm: ``_next`` is the earliest monotonic time at
    which the budget allows another request.  ``burst`` requests may go at
    once, after which the rate is ``rate`` per second.  ``take`` sleeps only
    as long as the budget says; it never sleeps a flat second "just in case".
    """

    def __init__(
        self,
        rate: float = 8.0,
        burst: int = 8,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.rate = max(float(rate), 0.001)
        self.burst = max(int(burst), 1)
        self._interval = 1.0 / self.rate
        self._credit_window = self._interval * (self.burst - 1)
        self._clock = clock
        self._sleep = sleep
        self._next = 0.0
        self._lock = threading.Lock()
        self.waited = 0.0

    def take(self, tokens: int = 1) -> float:
        """Wait until the budget allows *tokens* requests; return seconds waited."""
        with self._lock:
            now = self._clock()
            floor = now - self._credit_window
            if self._next < floor:
                self._next = floor
            wait = self._next - now
            # Sub-millisecond waits are floating-point noise, not politeness:
            # sleeping for them costs more than it buys.
            if wait < 1e-4:
                wait = 0.0
            self._next += self._interval * tokens
            self.waited += wait
        if wait > 0.0:
            self._sleep(wait)
        return wait
