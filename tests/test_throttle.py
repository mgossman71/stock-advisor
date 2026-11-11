"""Rate limiting + retry/backoff behaviour (yfinance throttles if we don't)."""

from __future__ import annotations

import pytest

from app.providers.throttle import (
    HostThrottler, PermanentProviderError, TransientProviderError,
    call_with_retries, compute_backoff_seconds,
)


class FakeClock:
    """Deterministic clock + sleeper so tests never actually sleep."""

    def __init__(self, now: float = 0.0) -> None:
        self.now = now
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def test_throttler_waits_the_full_interval_between_calls():
    clock = FakeClock()
    throttler = HostThrottler(1.5)
    assert throttler.wait("api.example.com", clock=clock, sleeper=clock.sleep) == 0.0
    throttler.wait("api.example.com", clock=clock, sleeper=clock.sleep)
    assert clock.slept == [1.5]


def test_throttler_gates_each_host_independently():
    clock = FakeClock()
    throttler = HostThrottler(2.0)
    throttler.wait("a.example.com", clock=clock, sleeper=clock.sleep)
    assert throttler.wait("b.example.com", clock=clock, sleeper=clock.sleep) == 0.0
    assert clock.slept == []


def test_throttler_only_sleeps_the_remaining_time():
    clock = FakeClock()
    throttler = HostThrottler(2.0)
    throttler.wait("h", clock=clock, sleeper=clock.sleep)
    clock.now += 1.5  # real work took 1.5 s
    remaining = throttler.wait("h", clock=clock, sleeper=clock.sleep)
    assert remaining == pytest.approx(0.5, abs=1e-9)


def test_throttler_disabled_with_zero_interval():
    clock = FakeClock()
    assert HostThrottler(0).wait("h", clock=clock, sleeper=clock.sleep) == 0.0
    assert clock.slept == []


def test_backoff_grows_exponentially_and_is_capped():
    without_jitter = [compute_backoff_seconds(i, base=1, ceiling=30, jitter=0) for i in range(7)]
    assert without_jitter == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]


def test_backoff_jitter_stays_within_bounds():
    for attempt in range(5):
        value = compute_backoff_seconds(attempt, base=1, ceiling=30)
        raw = min(30, 2 ** attempt)
        assert 0.75 * raw <= value <= 1.25 * raw


def test_call_with_retries_succeeds_after_transient_failures():
    clock = FakeClock()
    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise TransientProviderError("HTTP 429")
        return "ok"

    result = call_with_retries(flaky, retries=4, base=1, ceiling=30, sleeper=clock.sleep)
    assert result == "ok"
    assert attempts["n"] == 3
    assert clock.slept == [1.0, 2.0]


def test_call_with_retries_gives_up_and_reports_the_last_error():
    clock = FakeClock()

    def always_down():
        raise TransientProviderError("still 429")

    with pytest.raises(TransientProviderError, match="still 429"):
        call_with_retries(always_down, retries=2, base=1, ceiling=30, sleeper=clock.sleep)
    assert clock.slept == [1.0, 2.0]  # two waits, then the raise


def test_permanent_errors_are_not_retried():
    attempts = {"n": 0}

    def unknown_symbol():
        attempts["n"] += 1
        raise PermanentProviderError("HTTP 404")

    with pytest.raises(PermanentProviderError):
        call_with_retries(unknown_symbol, retries=4, sleeper=lambda _s: None)
    assert attempts["n"] == 1, "a 404 must not be retried four times"


def test_retry_hook_lets_callers_log_attempts():
    seen: list[int] = []
    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise TransientProviderError("429")
        return 1

    call_with_retries(
        flaky, retries=3, sleeper=lambda _s: None, on_retry=lambda attempt, _exc: seen.append(attempt)
    )
    assert seen == [0]
