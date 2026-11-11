"""Paper dividends: who is entitled, which way the cash moves, and that a
re-run can never book the same dividend twice."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal as D

import pytest
from sqlalchemy import func, select

from app.config import Settings
from app.db.models import DataFetchLog, Dividend, DividendPayment, Position, PriceHistory
from app.ingestion.ingest import ensure_ticker, ingest_dividends
from app.ingestion.splits import apply_split
from app.providers.base import Dividend as DividendPayload
from app.trading.accounts import get_or_open_account
from app.trading.dividends import book_dividends
from app.trading.orders import execute_order
from app.trading.snapshots import portfolio_values
from tests.conftest import FakeProvider

EX = dt.date(2026, 3, 13)
BEFORE = dt.date(2026, 3, 10)


@pytest.fixture
def settings() -> Settings:
    return Settings(initial_cash=D("100000"), commission_per_share=D("0"),
                    min_commission=D("0"), slippage_bps=D("0"))


def _bar(session, day: dt.date, close: str = "60") -> None:
    ticker = ensure_ticker(session, "KO")
    session.add(PriceHistory(ticker_id=ticker.id, date=day, close=D(close), source="fake"))
    session.flush()


def _dividend(session, ex: dt.date = EX, amount: str = "0.53") -> Dividend:
    row = Dividend(ticker_id=ensure_ticker(session, "KO").id, ex_date=ex,
                   amount=D(amount), source="fake")
    session.add(row)
    session.flush()
    return row


def _trade(session, account, settings, side: str, qty: int, day: dt.date) -> None:
    _bar(session, day)
    assert execute_order(session, account, "KO", side, qty, settings=settings).accepted


def test_long_holder_is_paid_once(session, settings):
    account = get_or_open_account(session, settings)
    _trade(session, account, settings, "buy", 10, BEFORE)
    _dividend(session)
    cash_before = account.cash_balance
    equity_before = portfolio_values(session, account)["total_value"]

    assert book_dividends(session, account, EX) == D("5.30")
    position = session.scalar(select(Position))
    assert account.cash_balance == cash_before + D("5.30")
    assert position.realized_pnl == D("5.30")
    assert portfolio_values(session, account)["total_value"] == equity_before + D("5.30")

    # The nightly job and /jobs/refresh both re-run this: nothing doubles.
    assert book_dividends(session, account, EX) == D("0")
    assert book_dividends(session, account, EX + dt.timedelta(days=1)) == D("0")
    assert account.cash_balance == cash_before + D("5.30")
    assert session.scalar(select(func.count()).select_from(DividendPayment)) == 1


def test_short_seller_pays_the_dividend(session, settings):
    account = get_or_open_account(session, settings)
    _trade(session, account, settings, "short", 10, BEFORE)
    _dividend(session)
    cash_before = account.cash_balance
    assert book_dividends(session, account, EX) == D("-5.30")
    assert account.cash_balance == cash_before - D("5.30")
    payment = session.scalar(select(DividendPayment))
    assert (payment.qty, payment.total) == (D("-10"), D("-5.30"))


def test_buying_on_the_ex_date_is_not_entitled(session, settings):
    account = get_or_open_account(session, settings)
    _dividend(session)
    _trade(session, account, settings, "buy", 10, EX)
    assert book_dividends(session, account, EX) == D("0")
    assert session.scalar(select(func.count()).select_from(DividendPayment)) == 0


def test_selling_on_the_ex_date_is_still_entitled(session, settings):
    account = get_or_open_account(session, settings)
    _trade(session, account, settings, "buy", 10, BEFORE)
    _dividend(session)
    _trade(session, account, settings, "sell", 10, EX)
    assert session.scalar(select(Position)).qty == D("0")
    assert book_dividends(session, account, EX) == D("5.30")


def test_partial_add_on_ex_date_pays_only_the_old_shares(session, settings):
    account = get_or_open_account(session, settings)
    _trade(session, account, settings, "buy", 10, BEFORE)
    _dividend(session)
    _trade(session, account, settings, "buy", 5, EX)
    assert book_dividends(session, account, EX) == D("5.30")  # 10 shares, not 15


def test_ex_dates_past_the_lookback_are_not_back_filled(session, settings):
    account = get_or_open_account(session, settings)
    _trade(session, account, settings, "buy", 10, BEFORE)
    _dividend(session)
    assert book_dividends(session, account, EX + dt.timedelta(days=45)) == D("0")


def test_future_ex_dates_wait_for_their_day(session, settings):
    account = get_or_open_account(session, settings)
    _trade(session, account, settings, "buy", 10, BEFORE)
    _dividend(session)
    assert book_dividends(session, account, EX - dt.timedelta(days=1)) == D("0")
    assert book_dividends(session, account, EX) == D("5.30")


def test_ingest_dividends_is_idempotent_and_a_non_payer_is_not_an_error(session):
    payer = FakeProvider(dividends=[DividendPayload("KO", EX, D("0.53"), "fake")])
    start, end = dt.date(2026, 1, 1), dt.date(2026, 12, 31)
    assert ingest_dividends(session, payer, "KO", start, end).written == 1
    again = ingest_dividends(session, payer, "KO", start, end)
    assert again.written == 0 and again.skipped == 1
    assert session.scalar(select(func.count()).select_from(Dividend)) == 1

    ingest_dividends(session, FakeProvider(dividends=[]), "TSLA", start, end)
    statuses = session.scalars(
        select(DataFetchLog.status).where(DataFetchLog.ticker == "TSLA")).all()
    assert statuses == ["empty"]  # pays nothing: logged, but not an error


def test_a_split_rescales_dividends_per_share(session):
    ticker = ensure_ticker(session, "KO")
    old = _dividend(session, ex=dt.date(2026, 3, 13), amount="2.00")
    new = _dividend(session, ex=dt.date(2026, 9, 15), amount="0.55")
    apply_split(session, ticker.id, dt.date(2026, 6, 1), 1, 4, source="fake")
    assert old.amount == D("0.5")    # pre-split dividend on the new share basis
    assert new.amount == D("0.55")   # already post-split: untouched
