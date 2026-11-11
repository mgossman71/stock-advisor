"""Ingestion of non-price data: quotes, fundamentals, news, analysts, earnings.

The invariant under test: a value the provider did not supply stays ``NULL``.
Nothing here may default a missing number to zero, because a zero would render
as a real figure and feed straight into the scoring engine.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import func, select

from app.db.models import Fundamentals, PriceHistory
from app.ingestion.ingest import ingest_fundamentals, ingest_quote
from app.providers.base import FundamentalsSnapshot, Quote
from tests.conftest import FakeProvider

NOW = dt.datetime(2026, 1, 9, 21, 0, tzinfo=dt.timezone.utc)


def test_quote_only_ingest_leaves_ohlc_null(session):
    """A live quote prices the day; it must not fake an open/high/low."""
    provider = FakeProvider(quote=Quote(
        symbol="AAPL", price=Decimal("348.51"), as_of=NOW, source="fake",
    ))
    stats, quote = ingest_quote(session, provider, "AAPL")
    session.flush()
    assert stats.written == 1 and quote is not None
    row = session.scalar(select(PriceHistory))
    assert row.close == Decimal("348.51")
    assert (row.open, row.high, row.low, row.volume, row.adj_close) == (None,) * 5
    assert row.date == dt.date(2026, 1, 9)


def test_two_quotes_on_the_same_day_update_one_row(session):
    """Intraday refreshes must not stack duplicate rows for one date."""
    ingest_quote(session, FakeProvider(quote=Quote(
        symbol="AAPL", price=Decimal("100.10"), as_of=NOW)), "AAPL")
    session.commit()
    ingest_quote(session, FakeProvider(quote=Quote(
        symbol="AAPL", price=Decimal("100.55"), as_of=NOW)), "AAPL")
    session.flush()
    assert session.scalar(select(func.count()).select_from(PriceHistory)) == 1
    assert session.scalar(select(PriceHistory)).close == Decimal("100.55")


def test_a_failed_quote_stores_no_price_and_logs_the_failure(session):
    from app.db.models import DataFetchLog

    stats, quote = ingest_quote(session, FakeProvider(fail={"quote"}), "AAPL")
    session.flush()
    assert quote is None and stats.written == 0
    assert session.scalar(select(func.count()).select_from(PriceHistory)) == 0
    assert session.scalar(select(DataFetchLog)).status == "error"


def test_fundamentals_snapshot_persists_and_enriches_the_ticker(session):
    snapshot = FundamentalsSnapshot(
        symbol="AAPL", as_of_date=dt.date(2026, 1, 9), source="yfinance",
        name="Apple Inc.", sector="Technology", pe_trailing=Decimal("38.80958"),
        return_on_equity=Decimal("142.257"), debt_to_equity=Decimal("0.84286"),
        target_price_consensus=None,
    )
    stats = ingest_fundamentals(session, FakeProvider(fundamentals=snapshot), "AAPL")
    session.flush()
    assert stats.written == 1
    assert any(
        "fields unavailable" in note and "target_price_consensus" in note
        for note in stats.notes
    ), "the ingester must surface exactly which fields were missing"
    row = session.scalar(select(Fundamentals))
    assert row.pe_trailing == Decimal("38.80958")
    assert row.target_price_consensus is None, "missing must stay NULL, never 0"


def test_fundamentals_for_the_same_as_of_date_are_upserted(session):
    ingest_fundamentals(session, FakeProvider(fundamentals=FundamentalsSnapshot(
        symbol="AAPL", as_of_date=dt.date(2026, 1, 9), pe_trailing=Decimal("30"),
        source="yfinance")), "AAPL")
    session.commit()
    ingest_fundamentals(session, FakeProvider(fundamentals=FundamentalsSnapshot(
        symbol="AAPL", as_of_date=dt.date(2026, 1, 9), pe_trailing=Decimal("31"),
        source="nasdaq")), "AAPL")
    session.flush()
    assert session.scalar(select(func.count()).select_from(Fundamentals)) == 1
    assert session.scalar(select(Fundamentals)).pe_trailing == Decimal("31")


def test_fundamentals_never_overwrite_good_values_with_missing_ones(session):
    """A thinner fallback snapshot must not blank an earlier complete one."""
    ingest_fundamentals(session, FakeProvider(fundamentals=FundamentalsSnapshot(
        symbol="AAPL", as_of_date=dt.date(2026, 1, 9), pe_trailing=Decimal("30"),
        source="yfinance")), "AAPL")
    session.commit()
    ingest_fundamentals(session, FakeProvider(fundamentals=FundamentalsSnapshot(
        symbol="AAPL", as_of_date=dt.date(2026, 1, 9), source="nasdaq")), "AAPL")
    session.flush()
    assert session.scalar(select(Fundamentals)).pe_trailing == Decimal("30")
