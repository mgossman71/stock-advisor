"""Grading math pinned to fixtures (acceptance criterion #5): 30d alpha etc."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal as D

import pytest

from app.db.models import PriceHistory, Recommendation, RecommendationOutcome
from app.ingestion.ingest import ensure_ticker
from app.research.outcomes import grade_recommendation, scoreboard, update_statuses

DAY0 = dt.date(2026, 1, 1)


def _bars(session, symbol: str, schedule: list[tuple[dt.date, D]]) -> None:
    ticker = ensure_ticker(session, symbol)
    for day, close in schedule:
        session.add(PriceHistory(ticker_id=ticker.id, date=day, close=close,
                                 low=close, high=close, source="fake"))
    session.flush()


def _rec(session, symbol: str, direction: str, entry: D, when: dt.date) -> Recommendation:
    ticker = ensure_ticker(session, symbol)
    rec = Recommendation(
        ticker_id=ticker.id, direction=direction, horizon="medium",
        generated_at=dt.datetime(when.year, when.month, when.day, tzinfo=dt.timezone.utc),
        entry_price_at_call=entry, status="open", confidence=D("60"), rationale="test",
    )
    session.add(rec)
    session.flush()
    return rec


@pytest.fixture
def seeded(session):
    """SPX: 100 until day 30 then 105 -> benchmark_pct_30d = +5 % exactly."""
    flat = [(DAY0 + dt.timedelta(days=i), D("100")) for i in range(30)]
    up = [(DAY0 + dt.timedelta(days=i), D("105")) for i in range(30, 61)]
    _bars(session, "SPX", flat + up)
    return session


def test_long_alpha_30d(seeded):
    """Entry 50, jumps to 60 at day 30: outcome +20 %, benchmark +5 %, alpha +15 %."""
    _bars(seeded, "WIN", [(DAY0 + dt.timedelta(days=i), D("50")) for i in range(30)] +
          [(DAY0 + dt.timedelta(days=i), D("60")) for i in range(30, 61)])
    rec = _rec(seeded, "WIN", "long", D("50"), DAY0)
    outcome = grade_recommendation(seeded, rec, benchmark_symbol="SPX",
                                   today=DAY0 + dt.timedelta(days=40))
    assert outcome.price_at_30d == D("60")
    assert outcome.outcome_pct_30d == D("20.0000")
    assert outcome.benchmark_pct_30d == D("5.0000")
    assert outcome.alpha_30d == D("15.0000")
    # 90-day window has not elapsed -> stays None, never extrapolated.
    assert outcome.price_at_90d is None and outcome.alpha_90d is None


def test_short_alpha_is_direction_correct(seeded):
    """Short 80 -> 72 is a +10 % signed outcome; alpha +5 % vs SPX."""
    _bars(seeded, "FADE", [(DAY0 + dt.timedelta(days=i), D("80")) for i in range(30)] +
          [(DAY0 + dt.timedelta(days=i), D("72")) for i in range(30, 61)])
    rec = _rec(seeded, "FADE", "short", D("80"), DAY0)
    outcome = grade_recommendation(seeded, rec, benchmark_symbol="SPX",
                                   today=DAY0 + dt.timedelta(days=40))
    assert outcome.outcome_pct_30d == D("10.0000")
    assert outcome.alpha_30d == D("5.0000")


def test_grading_is_idempotent(seeded):
    _bars(seeded, "WIN", [(DAY0 + dt.timedelta(days=i), D("50")) for i in range(30)] +
          [(DAY0 + dt.timedelta(days=i), D("60")) for i in range(30, 61)])
    rec = _rec(seeded, "WIN", "long", D("50"), DAY0)
    first = grade_recommendation(seeded, rec, benchmark_symbol="SPX",
                                 today=DAY0 + dt.timedelta(days=40))
    again = grade_recommendation(seeded, rec, benchmark_symbol="SPX",
                                 today=DAY0 + dt.timedelta(days=40))
    assert seeded.query(RecommendationOutcome).count() == 1
    assert first.alpha_30d == again.alpha_30d == D("15.0000")


def test_hit_stop_and_hit_target_from_bar_scan(seeded):
    _bars(seeded, "CRASH", [(DAY0 + dt.timedelta(days=i), D("50")) for i in range(10)] +
          [(DAY0 + dt.timedelta(days=10), D("44"))])
    rec = _rec(seeded, "CRASH", "long", D("50"), DAY0)
    rec.stop_price, rec.target_price = D("45"), D("70")
    seeded.flush()
    counts = update_statuses(seeded, today=DAY0 + dt.timedelta(days=11))
    assert counts["hit_stop"] == 1
    assert rec.status == "hit_stop"

    _bars(seeded, "MOON", [(DAY0 + dt.timedelta(days=i), D("50")) for i in range(9)] +
          [(DAY0 + dt.timedelta(days=9), D("70"))])
    good = _rec(seeded, "MOON", "long", D("50"), DAY0)
    good.stop_price, good.target_price = D("40"), D("70")
    seeded.flush()
    counts = update_statuses(seeded, today=DAY0 + dt.timedelta(days=11))
    assert counts["hit_target"] == 1 and good.status == "hit_target"


def test_stale_open_calls_expire(seeded):
    rec = _rec(seeded, "ZZZ", "long", D("10"), DAY0 - dt.timedelta(days=800))
    update_statuses(seeded, today=DAY0)
    assert rec.status == "expired"


def test_scoreboard_hit_rate(seeded):
    _bars(seeded, "WIN", [(DAY0 + dt.timedelta(days=i), D("50")) for i in range(30)] +
          [(DAY0 + dt.timedelta(days=i), D("60")) for i in range(30, 61)])
    _bars(seeded, "LOSE", [(DAY0 + dt.timedelta(days=i), D("50")) for i in range(30)] +
          [(DAY0 + dt.timedelta(days=i), D("45")) for i in range(30, 61)])
    r1 = _rec(seeded, "WIN", "long", D("50"), DAY0)
    r2 = _rec(seeded, "LOSE", "long", D("50"), DAY0)
    for rec in (r1, r2):
        grade_recommendation(seeded, rec, benchmark_symbol="SPX",
                             today=DAY0 + dt.timedelta(days=40))
    board = scoreboard(seeded)
    overall = board.overall[30]
    assert overall["graded"] == 2
    assert overall["hit_rate_pct"] == D("50.0")  # WIN alpha +15, LOSE alpha -5
