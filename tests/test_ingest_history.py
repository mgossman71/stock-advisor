"""Ingestion of price history: idempotent upserts, freshness cache, logging.

Re-running the nightly job or hammering "refresh now" must never duplicate rows
or invent prices - that is what these tests lock down.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import func, select

from app.db.models import DataFetchLog, PriceHistory, Ticker
from app.ingestion.ingest import covered_range, ensure_ticker, ingest_history
from tests.conftest import FakeProvider, make_bar

START = dt.date(2026, 1, 2)
MID = START + dt.timedelta(days=1)
END = dt.date(2026, 1, 9)
BARS = [
    make_bar(date=START, close="100.00", open="99", high="101", low="98", volume=1000),
    make_bar(date=MID, close="101.50"),
    make_bar(date=END, close="102.25"),
]


def _provider(**kwargs) -> FakeProvider:
    return FakeProvider(bars=BARS, **kwargs)


def test_ensure_ticker_is_idempotent_and_never_blanks_existing_values(session):
    first = ensure_ticker(session, "aapl", name="Apple Inc.", sector="Technology")
    again = ensure_ticker(session, "AAPL")
    assert first.id == again.id
    assert again.symbol == "AAPL"
    assert again.name == "Apple Inc.", "a later fetch without a name must not wipe it"
    assert session.scalar(select(func.count()).select_from(Ticker)) == 1


def test_ingest_history_writes_bars_and_logs_one_fetch(session):
    stats = ingest_history(session, _provider(), "AAPL", START, END)
    session.flush()
    assert stats.written == 3
    assert {row.date for row in session.scalars(select(PriceHistory))} == {START, MID, END}
    logs = session.scalars(select(DataFetchLog)).all()
    assert len(logs) == 1 and logs[0].status == "ok" and logs[0].row_count == 3


def test_ingest_history_skips_a_range_that_is_already_fresh(session):
    ingest_history(session, _provider(), "AAPL", START, END)
    session.commit()
    second = ingest_history(session, _provider(), "AAPL", START, END)
    session.flush()
    assert second.skipped == 3 and second.written == 0
    assert session.scalar(select(func.count()).select_from(PriceHistory)) == 3, "no duplicates"
    assert session.scalars(select(DataFetchLog.status)).all() == ["ok", "skipped_fresh"]


def test_a_range_that_does_not_reach_end_is_refetched(session):
    """Freshness is relative to `end`: stale coverage triggers a new fetch."""
    ingest_history(session, _provider(), "AAPL", START, END)
    session.commit()
    second = ingest_history(
        session, _provider(), "AAPL", START, END + dt.timedelta(days=10), freshness_days=1
    )
    session.flush()
    assert second.written == 3
    assert session.scalars(select(DataFetchLog.status)).all() == ["ok", "ok"]


def test_reingesting_a_range_updates_bars_in_place(session):
    """A corrected bar replaces the old one instead of piling up a second row."""
    ingest_history(session, _provider(), "AAPL", START, END)
    session.commit()
    revised = [
        make_bar(date=START, close="105.00"),
        make_bar(date=MID, close="101.50"),
        make_bar(date=END, close="102.25"),
    ]
    ingest_history(
        session, FakeProvider(bars=revised, name="nasdaq"), "AAPL",
        START, END + dt.timedelta(days=3), freshness_days=1,
    )
    session.flush()
    assert session.scalar(select(func.count()).select_from(PriceHistory)) == 3
    row = session.scalar(select(PriceHistory).where(PriceHistory.date == START))
    assert row.close == Decimal("105.00"), "a corrected bar must replace the old one"
    assert row.source == "nasdaq"


def test_covered_range_reports_stored_bounds(session):
    ingest_history(session, _provider(), "AAPL", START, END)
    session.flush()
    assert covered_range(session, 1, START, END) == (START, END, 3)


def test_covered_range_is_empty_for_an_unknown_ticker(session):
    assert covered_range(session, 999, START, END) == (None, None, 0)


def test_history_failure_logs_an_error_and_stores_nothing(session):
    provider = FakeProvider(bars=BARS, fail={"history"})
    stats = ingest_history(session, provider, "AAPL", START, END)
    session.flush()
    assert stats.written == 0 and "history unavailable" in stats.notes[0]
    log = session.scalar(select(DataFetchLog))
    assert log.status == "error" and "history down" in (log.error or "")
    assert session.scalar(select(func.count()).select_from(PriceHistory)) == 0


def test_adjusted_close_prefers_adj_and_falls_back_to_close(session):
    """Readers use ``adjusted_close``; storage keeps NULL honest (no copying)."""
    bars = [
        make_bar(date=START, close="100.00", adj_close=Decimal("95.00")),
        make_bar(date=MID, close="101.50"),
    ]
    ingest_history(session, FakeProvider(bars=bars), "AAPL", START, MID)
    session.flush()
    with_adj = session.scalar(select(PriceHistory).where(PriceHistory.date == START))
    without_adj = session.scalar(select(PriceHistory).where(PriceHistory.date == MID))
    assert with_adj.adjusted_close == Decimal("95.00")
    assert without_adj.adj_close is None, "never copy close into adj_close on the way in"
    assert without_adj.adjusted_close == Decimal("101.50"), "display falls back to close"
