"""Status reporting used by `app.cli stats` and the web status page.

The web layer must not disagree with the CLI, and a half-migrated database must
produce a report instead of an exception - both are locked down here.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.models import DataFetchLog, NewsItem, PriceHistory, Ticker
from app.ingestion.ingest import ensure_ticker
from app.ingestion.stats import coverage_by_ticker, recent_fetch_log, table_counts

D1 = dt.date(2026, 1, 2)
D2 = dt.date(2026, 1, 3)
D3 = dt.date(2026, 1, 9)


def _add_bars(session, symbol: str, dates) -> Ticker:
    ticker = ensure_ticker(session, symbol, name=f"{symbol} Inc.")
    for day in dates:
        session.add(
            PriceHistory(
                ticker_id=ticker.id,
                date=day,
                open=Decimal("100.00"),
                high=Decimal("101.00"),
                low=Decimal("99.00"),
                close=Decimal("100.50"),
                adj_close=Decimal("100.50"),
                volume=1000,
                source="yfinance",
            )
        )
    return ticker


def test_table_counts_reports_every_created_table(session):
    _add_bars(session, "AAPL", [D1, D2])
    session.add(
        DataFetchLog(provider="yfinance", endpoint="history(AAPL)", ticker="AAPL", status="ok", row_count=2)
    )
    session.flush()

    counts = table_counts(session)
    assert counts["tickers"] == 1
    assert counts["price_history"] == 2
    assert counts["data_fetch_log"] == 1
    assert counts["news_items"] == 0, "an empty table is reported as 0, not omitted"


def test_coverage_pairs_bar_span_with_side_data(session):
    _add_bars(session, "AAPL", [D1, D2, D3])
    _add_bars(session, "MSFT", [D3])
    aapl = session.scalar(select(Ticker).where(Ticker.symbol == "AAPL"))
    session.add_all(
        [
            NewsItem(ticker_id=aapl.id, title="Earnings beat", url="https://example.com/a", published_at=dt.datetime(2026, 1, 9, 12)),
            NewsItem(ticker_id=aapl.id, title="Analyst raises target", url="https://example.com/b"),
        ]
    )
    session.flush()

    rows = coverage_by_ticker(session)
    assert [r["symbol"] for r in rows] == ["AAPL", "MSFT"], "ordered by bar count desc"

    aapl_row = rows[0]
    assert aapl_row["bars"] == 3
    assert (aapl_row["first_date"], aapl_row["last_date"]) == ("2026-01-02", "2026-01-09")
    assert aapl_row["news"] == 2
    assert aapl_row.get("fundamentals", 0) == 0, "no fundamentals row means 0, not a fabricated value"

    msft_row = rows[1]
    assert msft_row["bars"] == 1 and msft_row["first_date"] == msft_row["last_date"] == "2026-01-09"


def test_coverage_is_empty_when_price_history_has_no_rows(session):
    assert coverage_by_ticker(session) == []


def test_fetch_log_is_newest_first_and_keeps_failures(session):
    session.add_all(
        [
            DataFetchLog(provider="yfinance", endpoint="history(AAPL)", ticker="AAPL", status="ok", row_count=10),
            DataFetchLog(provider="nasdaq", endpoint="history(AAPL)", ticker="AAPL", status="error", error="403"),
        ]
    )
    session.flush()

    entries = recent_fetch_log(session, limit=5)
    assert len(entries) == 2
    assert entries[0]["endpoint"] == "history(AAPL)"
    assert {"nasdaq", "yfinance"} == {e["provider"] for e in entries}
    failures = [e for e in entries if e["status"] == "error"]
    assert failures and failures[0]["error"] == "403", "failures must stay visible in status"
    assert all(e["rows"] == (10 if e["status"] == "ok" else 0) for e in entries)


def test_half_migrated_database_reports_only_existing_tables():
    """Only `tickers` exists: the report degrades instead of raising."""
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine, tables=[Ticker.__table__])
    session = sessionmaker(bind=engine, future=True)()
    try:
        session.add(Ticker(symbol="AAPL"))
        session.flush()

        counts = table_counts(session)
        assert counts == {"tickers": 1}, f"missing tables are omitted, got {counts}"
        assert coverage_by_ticker(session) == []
        assert recent_fetch_log(session) == []
        assert (
            session.scalar(select(func.count()).select_from(Ticker)) == 1
        ), "reporting must not take out a second connection and lose in-memory rows"
    finally:
        session.close()
        engine.dispose()


@pytest.mark.parametrize("limit", [1, 2])
def test_fetch_log_limit_is_respected(session, limit):
    for i in range(3):
        session.add(DataFetchLog(provider="yfinance", endpoint=f"news(T{i})", status="ok", row_count=1))
    session.flush()
    assert len(recent_fetch_log(session, limit=limit)) == limit
