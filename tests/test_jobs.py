"""Acceptance criterion #4: the nightly job is manually triggerable AND idempotent."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal as D

import pytest

from app.config import Settings
from app.db.models import (
    Alert, DataFetchLog, PortfolioSnapshot, PriceHistory, Recommendation,
)
from app.ingestion.ingest import ensure_ticker
from app.jobs import evaluate_alerts, run_nightly
from app.trading.accounts import get_or_open_account
from app.trading.orders import execute_order
from tests.conftest import FakeProvider, make_bar

DAY = dt.date(2026, 1, 5)


@pytest.fixture
def settings() -> Settings:
    return Settings(initial_cash=D("100000"), slippage_bps=D("0"),
                    commission_per_share=D("0"), min_commission=D("0"),
                    short_borrow_apy=D("0.365"), benchmark_symbol="^SPX",
                    vix_symbol="^VIX")


def _seed(session):
    """Universe ticker + benchmark, both with bars up to DAY."""
    t = ensure_ticker(session, "ACME", in_universe=True)
    bench = ensure_ticker(session, "^SPX", asset_class="index")
    ensure_ticker(session, "^VIX", asset_class="index")
    for i in range(80):
        day = DAY - dt.timedelta(days=80 - i)
        session.add(PriceHistory(ticker_id=t.id, date=day, close=D("100") + i,
                                 adj_close=D("100") + i, low=D("100") + i,
                                 high=D("100") + i, source="fake"))
        session.add(PriceHistory(ticker_id=bench.id, date=day, close=D("4000"),
                                 adj_close=D("4000"), low=D("4000"), high=D("4000"),
                                 source="fake"))
        session.add(PriceHistory(ticker_id=ensure_ticker(session, "^VIX").id, date=day,
                                 close=D("18"), adj_close=D("18"), low=D("18"),
                                 high=D("18"), source="fake"))
    session.flush()


def _provider() -> FakeProvider:
    bars = [make_bar("ACME", DAY, "180.00", low=D("180"), high=D("180"),
                     adj_close=D("180"))]
    return FakeProvider(bars=bars)


def test_run_nightly_twice_changes_nothing(session, settings):
    _seed(session)
    provider = _provider()

    first = run_nightly(session, provider, settings=settings, today=DAY)
    bars_after_first = session.query(PriceHistory).count()
    snaps_after_first = session.query(PortfolioSnapshot).count()
    assert first.snapshot_date == DAY.isoformat()
    assert first.fetched, "nightly pass must attempt fetches"

    second = run_nightly(session, _provider(), settings=settings, today=DAY)
    assert session.query(PriceHistory).count() == bars_after_first
    assert session.query(PortfolioSnapshot).count() == snaps_after_first == 1
    assert "borrow already accrued today - skipped" in second.notes


def test_borrow_accrues_once_per_day_on_a_short(session, settings):
    _seed(session)
    account = get_or_open_account(session, settings)
    execute_order(session, account, "ACME", "short", 10, settings=settings)
    session.commit()

    run_nightly(session, _provider(), settings=settings, today=DAY)
    from app.db.models import Position

    position = session.query(Position).one()
    charge_one_day = position.borrow_cost

    run_nightly(session, _provider(), settings=settings, today=DAY)
    assert position.borrow_cost == charge_one_day  # not double-charged

    # A *different* calendar day does charge again.
    run_nightly(session, _provider(), settings=settings,
                today=DAY + dt.timedelta(days=1))
    assert position.borrow_cost > charge_one_day


def test_alerts_trigger_from_stored_data(session, settings):
    _seed(session)
    acme = ensure_ticker(session, "ACME")
    session.add(Alert(ticker_id=acme.id, type="price_above", threshold=D("150"),
                      message="placeholder"))
    session.add(Alert(ticker_id=acme.id, type="price_below", threshold=D("150"),
                      message="placeholder"))
    session.commit()
    triggered = evaluate_alerts(session, settings, today=DAY)
    assert triggered == 1
    above = session.query(Alert).filter_by(type="price_above").one()
    assert above.triggered_at is not None
    assert above.observed_value == D("179")  # last stored close from _seed


def test_status_rows_are_logged(session, settings):
    _seed(session)
    run_nightly(session, _provider(), settings=settings, today=DAY)
    systems = session.query(DataFetchLog).filter(DataFetchLog.provider == "system").all()
    assert len(systems) == 1  # exactly one short_borrow marker for the day


def test_real_ledger_holdings_are_refreshed_nightly(session, settings):
    """A real holding outside the universe/watchlist must still get prices."""
    from app.jobs import target_symbols
    from app.tracking import record_trade

    _seed(session)
    record_trade(session, "OWNED", "buy", D("3"), D("50"), trade_date=DAY)
    record_trade(session, "SOLD", "buy", D("3"), D("50"), trade_date=DAY)
    record_trade(session, "SOLD", "sell", D("3"), D("55"), trade_date=DAY)
    symbols = target_symbols(session, settings)
    assert "OWNED" in symbols
    assert "SOLD" not in symbols  # fully closed: nothing to mark


def test_nightly_books_dividends_once(session, settings):
    from app.db.models import Dividend

    _seed(session)
    account = get_or_open_account(session, settings)
    assert execute_order(session, account, "ACME", "buy", 10, settings=settings).accepted
    acme = ensure_ticker(session, "ACME")
    # Ex-date tomorrow relative to the fill: the fill was at the DAY-1 bar.
    session.add(Dividend(ticker_id=acme.id, ex_date=DAY, amount=D("1.25"), source="fake"))
    session.commit()

    first = run_nightly(session, _provider(), settings=settings, today=DAY)
    assert first.dividends_booked == D("12.50")
    second = run_nightly(session, _provider(), settings=settings, today=DAY)
    assert second.dividends_booked == D("0")
