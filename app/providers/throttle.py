"""Rate limiting + retry/backoff for external market-data calls.

yfinance gets throttled hard when hammered, so two guards are always on:

1. :class:`HostThrottler` enforces a minimum interval between calls to the same
   host (default 1.2 s, configurable via ``MIN_PROVIDER_INTERVAL_SECONDS``).
2. :func:`call_with_retries` adds capped exponential backoff with jitter on
   transient failures (HTTP 429/5xx, timeouts, connection resets).
"""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


class TransientProviderError(RuntimeError):
    """Raised by providers for retryable failures (429 / 5xx / timeout)."""


class PermanentProviderError(RuntimeError):
    """Raised for non-retryable failures (unknown symbol, bad request)."""


class HostThrottler:
    """Blocks until ``min_interval_seconds`` has elapsed since the last call.

    Thread-safe: the APScheduler worker and the web worker share one instance.
    """

    def __init__(self, min_interval_seconds: float = 1.2) -> None:
        self.min_interval_seconds = float(min_interval_seconds)
        self._last_call: dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, host: str, *, clock: Callable[[], float] = time.monotonic,
             sleeper: Callable[[float], None] = time.sleep) -> float:
        """Sleep the required amount for ``host``; returns seconds slept."""
        if self.min_interval_seconds <= 0:
            return 0.0
        with self._lock:
            now = clock()
            last = self._last_call.get(host)
            delay = 0.0 if last is None else self.min_interval_seconds - (now - last)
            self._last_call[host] = now + max(delay, 0.0)
        if delay > 0:
            sleeper(delay)
        return max(delay, 0.0)


def compute_backoff_seconds(
    attempt: int,
    *,
    base: float = 1.0,
    ceiling: float = 30.0,
    jitter: float | None = None,
) -> float:
    """Exponential backoff for retry ``attempt`` (0-based), capped at ``ceiling``.

    Jitter in ``[0.75x, 1.25x]`` spreads retries so a throttled host is not
    re-hit in lockstep.  ``jitter`` may be pinned (0.0 disables it) for tests.
    """
    raw = min(ceiling, base * (2 ** max(0, attempt)))
    rng = random.Random(0) if jitter is None else None
    factor = (rng.uniform(0.75, 1.25) if rng else (1.0 if jitter == 0 else 1 + jitter))
    return round(raw * factor, 4)


def call_with_retries(
    func: Callable[[], T],
    *,
    retries: int = 4,
    base: float = 1.0,
    ceiling: float = 30.0,
    sleeper: Callable[[float], None] = time.sleep,
    on_retry: Callable[[int, Exception], None] | None = None,
) -> T:
    """Run ``func`` with exponential backoff on :class:`TransientProviderError`.

    Non-transient exceptions propagate immediately.  The final transient error
    is re-raised once retries are exhausted so callers can log it honestly.
    """
    attempt = 0
    while True:
        try:
            return func()
        except TransientProviderError as exc:
            if attempt >= retries:
                raise
            if on_retry is not None:
                on_retry(attempt, exc)
            sleeper(compute_backoff_seconds(attempt, base=base, ceiling=ceiling, jitter=0.0))
            attempt += 1
