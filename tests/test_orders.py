"""End-to-end order execution against the DB (acceptance criterion #3).

Buy 10 shares -> position, cash, P&L update; sell -> correct realised P&L.
Plus the rejection paths: unknown ticker, no stored price, insufficient cash,
closing more than is open.  Zero slippage/commission unless a test opts in.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.config import Settings
from app.db.models import Account, Order, Position, PriceHistory, Ticker
from app.ingestion.ingest import ensure_ticker
from app.trading.accounts import get_or_open_account
from app.trading.orders import execute_order
from app.trading.snapshots import mark_positions, portfolio_values

D = Decimal
DAY = dt.date(2026, 1, 5)


@pytest.fixture
def settings() -> Settings:
    """Deterministic trading settings: no friction unless a test adds it."""
    return Settings(
        initial_cash=D("100000"), commission_per_share=D("0"), min_commission=D("0"),
        slippage_bps=D("0"), short_borrow_apy=D("0.0365"), risk_per_trade_pct=D("1"),
    )


def _seed_price(session, symbol: str, date: dt.date, close: Decimal) -> Ticker:
    ticker = ensure_ticker(session, symbol)
    session.add(PriceHistory(ticker_id=ticker.id, date=date, close=close, source="fake"))
    session.flush()
    return ticker


def test_buy_ten_shares_updates_cash_position_and_pnl(session, settings):
    """The exact acceptance scenario: buy 10, see position/cash/P&L; sell, see P&L."""
    _seed_price(session, "ACME", DAY, D("150.00"))
    account = get_or_open_account(session, settings)

    result = execute_order(session, account, "ACME", "buy", 10, settings=settings)
    assert result.accepted
    assert result.order.status == "filled"
    assert result.order.fill_price == D("150.00")
    assert account.cash_balance == D("98500.00")  # 100000 - 10*150

    position = session.query(Position).one()
    assert position.qty == D("10")
    assert position.avg_cost == D("150.00")
    assert position.last_price == D("150.00")
    assert position.unrealized_pnl == D("0")  # marked at the fill, flat - a *known* 0

    # Price moves to 160; marking makes the +100 unrealized appear.
    _seed_price(session, "ACME", DAY + dt.timedelta(days=1), D("160.00"))
    assert mark_positions(session, account) == 1
    assert position.unrealized_pnl == D("100")

    # Sell for a correct realised figure.
    sell = execute_order(session, account, "ACME", "sell", 10, settings=settings)
    assert sell.accepted
    assert position.qty == D("0")
    assert position.realized_pnl == D("100")
    assert account.cash_balance == D("100100.00")


def test_full_short_cycle_with_borrow(session, settings):
    """Short at 100, borrow accrues 1/day (apy=36.5% -> 365 days), cover at 90."""
    _seed_price(session, "DOWN", DAY, D("100.00"))
    account = get_or_open_account(session, settings)

    assert execute_order(session, account, "DOWN", "short", 10, settings=settings).accepted
    assert account.cash_balance == D("101000.00")  # proceeds credited
    position = session.query(Position).one()
    assert position.qty == D("-10")

    from app.trading.orders import accrue_short_borrow

    # 10 * 100 * 0.0365 / 365 = 0.10 per day.
    assert accrue_short_borrow(session, account, DAY, settings=settings) == D("0.10")
    assert position.borrow_cost == D("0.10")

    _seed_price(session, "DOWN", DAY + dt.timedelta(days=1), D("90.00"))
    mark_positions(session, account)
    assert position.unrealized_pnl == D("100")  # (100-90)*10 before borrow

    cover = execute_order(session, account, "DOWN", "cover", 10, settings=settings)
    assert cover.accepted
    assert position.realized_pnl == D("99.9")  # 100 price gain - 0.1 borrow


def test_slippage_moves_fill_against_trader(session, settings):
    friction = Settings(initial_cash=D("100000"), slippage_bps=D("100"),
                        commission_per_share=D("0.01"), min_commission=D("1"))
    _seed_price(session, "SLIP", DAY, D("100.00"))
    account = get_or_open_account(session, settings)
    result = execute_order(session, account, "SLIP", "buy", 10, settings=friction)
    assert result.order.fill_price == D("101.000000")  # 1 % against us
    assert result.order.commission == D("1")  # max(0.01*10, min 1) -> the floor wins
    assert result.order.slippage == D("10")  # 1.00 * 10 shares


def test_sell_more_than_open_is_rejected(session, settings):
    _seed_price(session, "LIMI", DAY, D("10.00"))
    account = get_or_open_account(session, settings)
    assert execute_order(session, account, "LIMI", "buy", 5, settings=settings).accepted
    bad = execute_order(session, account, "LIMI", "sell", 6, settings=settings)
    assert not bad.accepted
    assert "cannot sell 6" in bad.reason
    assert bad.order.status == "rejected"
    # Rejected attempt is kept in history, and nothing else moved.
    assert session.query(Order).filter(Order.status == "rejected").count() == 1
    position = session.query(Position).one()
    assert position.qty == D("5")
    assert account.cash_balance == D("99950.00")


def test_insufficient_cash_rejects_buy(session, settings):
    _seed_price(session, "BIG", DAY, D("50000.00"))
    account = get_or_open_account(session, settings)
    result = execute_order(session, account, "BIG", "buy", 3, settings=settings)
    assert not result.accepted
    assert "insufficient cash" in result.reason
    assert account.cash_balance == D("100000")
    assert session.query(Position).count() == 0


def test_unknown_ticker_and_missing_price_reject(session, settings):
    account = get_or_open_account(session, settings)
    unknown = execute_order(session, account, "NOPE", "buy", 1, settings=settings)
    assert not unknown.accepted and "unknown ticker" in unknown.reason
    ensure_ticker(session, "UNPRICED")
    no_price = execute_order(session, account, "UNPRICED", "buy", 1, settings=settings)
    assert not no_price.accepted and "no stored price" in no_price.reason


def test_limit_order_just_sits_pending_when_unreachable(session, settings):
    _seed_price(session, "PEND", DAY, D("100.00"))
    account = get_or_open_account(session, settings)
    result = execute_order(session, account, "PEND", "buy", 5,
                           limit_price=D("95"), settings=settings)
    assert not result.accepted
    assert result.order.status == "pending"
    assert account.cash_balance == D("100000")
    assert session.query(Position).count() == 0


def test_account_is_created_once(session, settings):
    get_or_open_account(session, settings)
    again = get_or_open_account(session, settings)
    assert session.query(Account).count() == 1
    assert again.cash_balance == D("100000")
