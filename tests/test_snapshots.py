"""Portfolio marking + daily snapshot math, including short equity correctness."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal as D

import pytest

from app.config import Settings
from app.db.models import PortfolioSnapshot
from app.ingestion.ingest import ensure_ticker
from app.trading.accounts import get_or_open_account
from app.trading.orders import execute_order
from app.trading.snapshots import (
    equity_curve,
    mark_positions,
    portfolio_values,
    write_snapshot,
)

DAY = dt.date(2026, 1, 5)


@pytest.fixture
def settings() -> Settings:
    return Settings(initial_cash=D("100000"), slippage_bps=D("0"),
                    commission_per_share=D("0"), min_commission=D("0"),
                    benchmark_symbol="SPX")


def _add_bar(session, ticker_id, date, close):
    from app.db.models import PriceHistory

    session.add(PriceHistory(ticker_id=ticker_id, date=date, close=close, source="fake"))
    session.flush()


def test_short_equity_math(session, settings):
    """Short 10 @ 100 then mark at 90: cash carries proceeds, value carries the short."""
    _add_bar(session, ensure_ticker(session, "SHORT").id, DAY, D("100"))
    account = get_or_open_account(session, settings)
    execute_order(session, account, "SHORT", "short", 10, settings=settings)
    _add_bar(session, ensure_ticker(session, "SHORT").id, DAY + dt.timedelta(days=1), D("90"))
    mark_positions(session, account)

    values = portfolio_values(session, account)
    assert values["cash"] == D("101000")
    assert values["positions_value"] == D("-900")
    assert values["total_value"] == D("100100")  # equity up 100 on the short
    assert values["short_exposure"] == D("900")
    assert values["unmarked"] == []


def test_snapshot_idempotent_and_chained(session, settings):
    """Re-running the snapshot for the same day updates in place; daily_pnl chains."""
    t = ensure_ticker(session, "EQUITY")
    _add_bar(session, t.id, DAY, D("10"))
    spx = ensure_ticker(session, "SPX")
    _add_bar(session, spx.id, DAY, D("4000"))
    account = get_or_open_account(session, settings)
    execute_order(session, account, "EQUITY", "buy", 100, settings=settings)

    first = write_snapshot(session, account, DAY, benchmark_symbol="SPX")
    again = write_snapshot(session, account, DAY, benchmark_symbol="SPX")
    rows = session.query(PortfolioSnapshot).all()
    assert len(rows) == 1 and first.id == again.id
    assert first.cash == D("99000")
    assert first.total_value == D("100000")
    assert first.spx_close == D("4000")
    assert first.total_return_pct == D("0")

    _add_bar(session, t.id, DAY + dt.timedelta(days=1), D("12"))
    mark_positions(session, account)
    second = write_snapshot(session, account, DAY + dt.timedelta(days=1),
                            benchmark_symbol="SPX")
    assert second.total_value == D("100200")
    assert second.daily_pnl == D("200")  # chained off the previous snapshot
    assert second.total_return_pct == D("0.2")  # vs initial cash

    curve = equity_curve(session)
    assert [c["date"] for c in curve] == [DAY.isoformat(), (DAY + dt.timedelta(days=1)).isoformat()]


def test_unmarked_position_is_surfaced_not_zeroed(session, settings):
    """A position whose ticker has no *new* bar keeps its last mark, explicitly."""
    t = ensure_ticker(session, "STALE")
    _add_bar(session, t.id, DAY, D("50"))
    account = get_or_open_account(session, settings)
    execute_order(session, account, "STALE", "buy", 10, settings=settings)
    values = portfolio_values(session, account)
    assert values["positions_value"] == D("500")  # still marked from the fill
    # A hand-made position with no price at all reports as unmarked (never 0).
    from app.db.models import Position

    t2 = ensure_ticker(session, "NOPRICE")
    session.add(Position(account_id=account.id, ticker_id=t2.id, qty=D("5"),
                         avg_cost=D("10")))
    session.flush()
    values = portfolio_values(session, account)
    assert values["unmarked"] == ["NOPRICE"]
