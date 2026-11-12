"""Grading the app's own advice: 30/90/180-day windows vs the benchmark.

A call's *signed* outcome (shorts already flipped):

    long:  (price_window / entry - 1) * 100
    short: (1 - price_window / entry) * 100

alpha is the call's edge over the market in the call's own direction, over
the **same calendar window** (the benchmark leg uses the benchmark close
on/before the call date as its base)::

    long:  alpha = stock_return - benchmark_return
    short: alpha = benchmark_return - stock_return   (= outcome + benchmark)

A short call claims the stock will *underperform*; grading it against a
long benchmark would mark every short a loser in a rising market even when
the stock lagged badly.  ``benchmark_pct_*`` stores the raw index return.  Windows that have not elapsed stay ``None`` - a half-open window is
"not yet gradeable", not a score.

Status transitions here are honest re-reads of stored bars (stop/target were
actually touched or they were not), and ``expire`` only fires on age.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    PriceHistory, Recommendation, RecommendationOutcome, Ticker,
)

D = Decimal
WINDOWS = (30, 90, 180)
EXPIRE_AFTER_DAYS = 730
#: Short-horizon calls (the emerging lens's breakouts) are stale much sooner.
EXPIRE_AFTER_DAYS_SHORT = 120


def _close_on_or_before(session: Session, ticker_id: int, day: dt.date) -> Decimal | None:
    return session.scalar(
        select(PriceHistory.close)
        .where(PriceHistory.ticker_id == ticker_id, PriceHistory.date <= day)
        .order_by(PriceHistory.date.desc()).limit(1)
    )


def _outcome_row(session: Session, rec: Recommendation) -> RecommendationOutcome:
    row = session.scalar(
        select(RecommendationOutcome).where(
            RecommendationOutcome.recommendation_id == rec.id
        )
    )
    if row is None:
        row = RecommendationOutcome(recommendation_id=rec.id)
        session.add(row)
        session.flush()
    return row


def _window_price(session: Session, ticker: Ticker, call_day: dt.date,
                  window_days: int) -> tuple[dt.date, Decimal] | None:
    """First close at or after ``call_day + window_days`` (None = not elapsed yet)."""
    target = call_day + dt.timedelta(days=window_days)
    row = session.execute(
        select(PriceHistory.date, PriceHistory.close)
        .where(PriceHistory.ticker_id == ticker.id, PriceHistory.date >= target)
        .order_by(PriceHistory.date.asc()).limit(1)
    ).first()
    return (row.date, row.close) if row else None


def grade_recommendation(
    session: Session, rec: Recommendation, *, benchmark_symbol: str,
    today: dt.date | None = None,
) -> RecommendationOutcome | None:
    """Fill every elapsed window on ``rec``'s outcome row; skip unfinished ones."""
    today = today or dt.date.today()
    call_day = rec.generated_at.date()
    bench = session.scalar(select(Ticker).where(Ticker.symbol == benchmark_symbol))
    bench_start = _close_on_or_before(session, bench.id, call_day) if bench else None

    entry = rec.entry_price_at_call
    if entry is None or entry == 0:
        return None  # cannot grade a call without an entry - never invent one

    row = _outcome_row(session, rec)
    signed = D("1") if rec.direction == "long" else D("-1")
    for window in WINDOWS:
        priced = _window_price(session, rec.ticker, call_day, window)
        if priced is None or priced[0] > today:
            continue  # window not elapsed in stored data
        _, price = priced
        outcome_pct = (signed * (price / entry - D("1")) * D("100")).quantize(D("0.0001"))
        setattr(row, f"price_at_{window}d", price)
        setattr(row, f"outcome_pct_{window}d", outcome_pct)
        if bench and bench_start:
            bench_priced = _window_price(session, bench, call_day, window)
            if bench_priced:
                bench_pct = (bench_priced[1] / bench_start - D("1")) * D("100")
                bench_pct = bench_pct.quantize(D("0.0001"))
                setattr(row, f"benchmark_price_at_{window}d", bench_priced[1])
                setattr(row, f"benchmark_pct_{window}d", bench_pct)
                setattr(row, f"alpha_{window}d",
                        (outcome_pct - signed * bench_pct).quantize(D("0.0001")))
    session.flush()
    return row


def update_statuses(session: Session, *, today: dt.date | None = None) -> dict[str, int]:
    """Touch-through stop/target detection + age-expiry over stored bars."""
    today = today or dt.date.today()
    counts = {"hit_stop": 0, "hit_target": 0, "expired": 0}
    open_recs = session.scalars(
        select(Recommendation).where(Recommendation.status == "open")
    ).all()
    for rec in open_recs:
        stop_hit = target_hit = None
        rows = session.execute(
            select(PriceHistory.date, PriceHistory.low, PriceHistory.high,
                   PriceHistory.close)
            .where(PriceHistory.ticker_id == rec.ticker_id,
                   PriceHistory.date > rec.generated_at.date())
            .order_by(PriceHistory.date)
        ).all()
        for row in rows:
            low = row.low if row.low is not None else row.close
            high = row.high if row.high is not None else row.close
            if rec.direction == "long":
                if rec.stop_price is not None and low <= rec.stop_price:
                    stop_hit = row.date
                    break
                if rec.target_price is not None and high >= rec.target_price:
                    target_hit = row.date
                    break
            else:
                if rec.stop_price is not None and high >= rec.stop_price:
                    stop_hit = row.date
                    break
                if rec.target_price is not None and low <= rec.target_price:
                    target_hit = row.date
                    break
        if stop_hit and (not target_hit or stop_hit <= target_hit):
            rec.status, rec.closed_at = "hit_stop", dt.datetime(
                stop_hit.year, stop_hit.month, stop_hit.day, tzinfo=dt.timezone.utc)
            counts["hit_stop"] += 1
        elif target_hit:
            rec.status, rec.closed_at = "hit_target", dt.datetime(
                target_hit.year, target_hit.month, target_hit.day, tzinfo=dt.timezone.utc)
            counts["hit_target"] += 1
        elif (today - rec.generated_at.date()).days > (
                EXPIRE_AFTER_DAYS_SHORT if rec.horizon == "short" else EXPIRE_AFTER_DAYS):
            rec.status, rec.closed_at = "expired", dt.datetime(
                today.year, today.month, today.day, tzinfo=dt.timezone.utc)
            counts["expired"] += 1
    session.flush()
    return counts


@dataclass
class Scoreboard:
    """Aggregate hit-rate/alpha over graded recommendations, per window."""

    rows: list[dict]

    @property
    def overall(self) -> dict:
        merged: dict[int, dict] = {}
        for row in self.rows:
            for window, stats in row.items():
                bucket = merged.setdefault(window, {"graded": 0, "wins": 0,
                                                    "alpha_sum": D("0"), "outcome_sum": D("0")})
                bucket["graded"] += stats["graded"]
                bucket["wins"] += stats["wins"]
                bucket["alpha_sum"] += stats["alpha_sum"]
                bucket["outcome_sum"] += stats["outcome_sum"]
        out = {}
        for window, bucket in merged.items():
            graded = bucket["graded"]
            out[window] = {
                "graded": graded,
                "hit_rate_pct": (D(bucket["wins"]) * 100 / D(graded)).quantize(D("0.1"))
                if graded else None,
                "avg_alpha_pct": (bucket["alpha_sum"] / D(graded)).quantize(D("0.01"))
                if graded else None,
                "avg_outcome_pct": (bucket["outcome_sum"] / D(graded)).quantize(D("0.01"))
                if graded else None,
            }
        return out


#: Scoreboard lenses: which engines' calls to grade together.
LENSES = ("emerging", "core")


def lens_filter(lens: str | None):
    """SQL condition on ``engine_version`` for a lens (None = everything):
    ``emerging`` = the emerging-opportunity calls, ``core`` = the rest."""
    if lens == "emerging":
        return Recommendation.engine_version.like("emerging%")
    if lens == "core":
        return Recommendation.engine_version.not_like("emerging%")
    return None


def scoreboard(session: Session, lens: str | None = None) -> Scoreboard:
    """Per-ticker grading table for the recommendations page, optionally for
    one lens so the emerging calls can prove (or disprove) themselves."""
    query = (
        select(Recommendation, RecommendationOutcome, Ticker.symbol)
        .join(RecommendationOutcome,
              RecommendationOutcome.recommendation_id == Recommendation.id)
        .join(Ticker, Ticker.id == Recommendation.ticker_id)
    )
    condition = lens_filter(lens)
    if condition is not None:
        query = query.where(condition)
    rows = session.execute(query).all()
    per_symbol: list[dict] = []
    for rec, outcome, symbol in rows:
        entry: dict = {"symbol": symbol, "direction": rec.direction,
                       "status": rec.status, "windows": {}}
        for window in WINDOWS:
            alpha = getattr(outcome, f"alpha_{window}d", None)
            if alpha is None:
                continue
            outcome_pct = getattr(outcome, f"outcome_pct_{window}d")
            stats = entry["windows"].setdefault(window, {"graded": 0, "wins": 0,
                                                         "alpha_sum": D("0"), "outcome_sum": D("0")})
            stats["graded"] += 1
            stats["wins"] += 1 if alpha > 0 else 0
            stats["alpha_sum"] += alpha
            stats["outcome_sum"] += outcome_pct or D("0")
        per_symbol.append(entry)
    return Scoreboard(rows=[e["windows"] for e in per_symbol if e["windows"]])
