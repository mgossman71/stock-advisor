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
from app.jobs import evaluate_alerts, refresh_prices, run_nightly
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


# --- quick price refresh -----------------------------------------------------

def _quote(symbol: str, price: str):
    from app.providers.base import Quote
    return Quote(symbol=symbol, price=D(price),
                 as_of=dt.datetime(DAY.year, DAY.month, DAY.day, 21, tzinfo=dt.timezone.utc))


class _QuoteBoard(FakeProvider):
    """Quotes per symbol; records which symbols were asked for."""

    def __init__(self, prices: dict[str, str]):
        super().__init__()
        self.prices, self.asked = prices, []

    def get_quote(self, symbol, *, asset_class="stock"):
        from app.providers.base import ProviderResult
        self.calls.append("quote")
        self.asked.append(symbol)
        if symbol not in self.prices:
            return ProviderResult(self.name, f"quote({symbol})", ok=False, error="no price")
        quote = _quote(symbol, self.prices[symbol])
        return ProviderResult(self.name, f"quote({symbol})", ok=True, data=(quote,),
                              row_count=1, as_of=quote.as_of)


def _price_on(session, symbol: str, day: dt.date) -> D | None:
    t = ensure_ticker(session, symbol)
    row = session.query(PriceHistory).filter_by(ticker_id=t.id, date=day).one_or_none()
    return row.close if row else None


def test_price_refresh_only_quotes_what_is_held_or_watched(session, settings):
    from app.db.models import Position

    _seed(session)                                   # ACME is universe-only
    ensure_ticker(session, "HELD", in_universe=True)
    ensure_ticker(session, "WATCH", in_watchlist=True)
    held = ensure_ticker(session, "HELD")
    session.add(PriceHistory(ticker_id=held.id, date=DAY - dt.timedelta(days=1),
                             close=D("50"), source="fake"))
    session.commit()
    account = get_or_open_account(session, settings)
    execute_order(session, account, "HELD", "buy", 10, settings=settings)
    session.commit()

    board = _QuoteBoard({"HELD": "55.50", "WATCH": "20", "^SPX": "4100"})
    report = refresh_prices(session, board, settings=settings, today=DAY)

    assert sorted(board.asked) == ["HELD", "WATCH", "^SPX"], "universe-only ACME is skipped"
    assert set(board.calls) == {"quote"}, "no history/fundamentals/news calls"
    assert sorted(report.quoted) == ["HELD", "WATCH", "^SPX"] and report.errors == []
    position = session.query(Position).filter_by(account_id=account.id).one()
    assert position.last_price == D("55.50")
    assert report.snapshot_date == DAY.isoformat()


def test_price_refresh_fills_limits_triggers_alerts_and_is_idempotent(session, settings):
    from app.db.models import Order

    _seed(session)
    account = get_or_open_account(session, settings)
    pending = execute_order(session, account, "ACME", "buy", 5, settings=settings,
                            limit_price=D("150"))
    assert pending.order.status == "pending"   # stored close 179 is above the limit
    acme = ensure_ticker(session, "ACME")
    session.add(Alert(ticker_id=acme.id, type="price_below", threshold=D("160"),
                      message="placeholder"))
    session.commit()

    board = _QuoteBoard({"ACME": "149.00", "^SPX": "4000"})
    report = refresh_prices(session, board, settings=settings, today=DAY)
    assert "ACME" in board.asked, "pending orders and alerts pull their ticker in"
    assert report.orders.get("filled") == 1 and report.alerts_triggered == 1
    assert session.get(Order, pending.order.id).status == "filled"
    assert _price_on(session, "ACME", DAY) == D("149.00")

    bars = session.query(PriceHistory).count()
    again = refresh_prices(session, _QuoteBoard({"ACME": "149.00", "^SPX": "4000"}),
                           settings=settings, today=DAY)
    assert again.orders.get("filled", 0) == 0 and again.alerts_triggered == 0
    assert session.query(PriceHistory).count() == bars
    assert session.query(PortfolioSnapshot).count() == 1


def test_price_refresh_reports_symbols_without_a_quote(session, settings):
    ensure_ticker(session, "GONE", in_watchlist=True)
    session.commit()
    report = refresh_prices(session, _QuoteBoard({}), settings=settings, today=DAY)
    assert report.quoted == [] and len(report.errors) == 2
    assert any(e.startswith("GONE:") for e in report.errors)


def test_target_symbols_is_price_symbols_plus_universe_and_vix(session, settings):
    from app.jobs import price_symbols, target_symbols

    _seed(session)
    ensure_ticker(session, "WATCH", in_watchlist=True)
    assert price_symbols(session, settings) == ["WATCH", "^SPX"]
    assert target_symbols(session, settings) == ["ACME", "WATCH", "^SPX", "^VIX"]
