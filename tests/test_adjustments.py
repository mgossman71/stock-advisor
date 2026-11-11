"""Cash adjustments: deposit/withdraw rules, overdraw rejection, and the
guarantee that moved cash never shows up as trading performance."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal as D

import pytest

from app.config import Settings
from app.db.models import CashAdjustment
from app.ingestion.ingest import ensure_ticker
from app.trading.accounts import CashAdjustmentError, adjust_cash, get_or_open_account
from app.trading.snapshots import mark_positions, write_snapshot

DAY = dt.date(2026, 1, 5)


@pytest.fixture
def settings() -> Settings:
    return Settings(initial_cash=D("100000"), slippage_bps=D("0"),
                    commission_per_share=D("0"), min_commission=D("0"),
                    benchmark_symbol="SPX")


def _stamp(adj: CashAdjustment, on: dt.date) -> CashAdjustment:
    """Pin an adjustment's created_at so snapshot date windows are testable."""
    adj.created_at = dt.datetime(on.year, on.month, on.day, 12, tzinfo=dt.timezone.utc)
    return adj


def test_deposit_and_withdraw_update_cash_only(session, settings):
    """Balances move, initial_cash (the performance base) does not."""
    account = get_or_open_account(session, settings)
    adj = adjust_cash(session, account, D("25000"), note="top-up")
    assert account.cash_balance == D("125000")
    assert account.initial_cash == D("100000")
    assert adj.delta == D("25000") and adj.note == "top-up"

    adjust_cash(session, account, D("-5000"))
    assert account.cash_balance == D("120000")

    rows = session.query(CashAdjustment).order_by(CashAdjustment.id).all()
    assert [r.delta for r in rows] == [D("25000"), D("-5000")]


def test_withdrawal_cannot_overdraw_and_zero_rejected(session, settings):
    account = get_or_open_account(session, settings)
    with pytest.raises(CashAdjustmentError):
        adjust_cash(session, account, D("-100000.01"))
    with pytest.raises(CashAdjustmentError):
        adjust_cash(session, account, D("0"))
    assert account.cash_balance == D("100000")  # untouched after rejects
    assert session.query(CashAdjustment).count() == 0

    adjust_cash(session, account, D("-100000"))  # exactly to zero is fine
    assert account.cash_balance == D("0")


def test_open_account_initial_cash_override(session, settings):
    """A brand-new account can start at a user-chosen balance."""
    account = get_or_open_account(session, settings, initial_cash=D("2500"))
    assert account.initial_cash == D("2500")
    assert account.cash_balance == D("2500")


def test_snapshot_excludes_adjustments_from_performance(session, settings):
    """Deposit 5,000 overnight: equity jumps, but daily_pnl and return % do not."""
    t = ensure_ticker(session, "ADJ")
    _add_bar(session, t.id, DAY, D("10"))
    spx = ensure_ticker(session, "SPX")
    _add_bar(session, spx.id, DAY, D("4000"))
    account = get_or_open_account(session, settings)
    from app.trading.orders import execute_order

    execute_order(session, account, "ADJ", "buy", 100, settings=settings)
    write_snapshot(session, account, DAY, benchmark_symbol="SPX")

    _add_bar(session, t.id, DAY + dt.timedelta(days=1), D("12"))
    _stamp(adjust_cash(session, account, D("5000")), DAY + dt.timedelta(days=1))
    mark_positions(session, account)
    second = write_snapshot(session, account, DAY + dt.timedelta(days=1),
                            benchmark_symbol="SPX")

    assert second.total_value == D("105200")       # equity includes the deposit
    assert second.daily_pnl == D("200")            # ... the day's P&L does not
    assert second.total_return_pct.quantize(D("0.001")) == D("0.190")  # base = 100k + 5k

    # A withdrawal is the mirror image.
    _add_bar(session, t.id, DAY + dt.timedelta(days=2), D("11"))
    _stamp(adjust_cash(session, account, D("-2000")), DAY + dt.timedelta(days=2))
    mark_positions(session, account)
    third = write_snapshot(session, account, DAY + dt.timedelta(days=2),
                           benchmark_symbol="SPX")
    assert third.total_value == D("103100")
    assert third.daily_pnl == D("-100")            # 12->11 mark drop only


def _add_bar(session, ticker_id, date, close):
    from app.db.models import PriceHistory

    session.add(PriceHistory(ticker_id=ticker_id, date=date, close=close, source="fake"))
    session.flush()
