"""Stale-price guard: a paper order never fills at an old close.

The clock is injected (``now=``) and the provider is the scriptable fake, so
weekends, holidays and the New York vs UTC date boundary are all pinned
without the network.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal as D

import pytest

from app.config import Settings
from app.db.models import PriceHistory
from app.ingestion.ingest import ensure_ticker
from app.providers.base import Quote
from app.trading.accounts import get_or_open_account
from app.trading.freshness import ensure_fresh_price, last_session_date, market_today
from app.trading.orders import execute_order
from tests.conftest import FakeProvider

UTC = dt.timezone.utc
TUE = dt.date(2026, 11, 10)


def _at(day: dt.date, hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC)


def _bar(session, symbol: str, day: dt.date, close: str):
    ticker = ensure_ticker(session, symbol)
    session.add(PriceHistory(ticker_id=ticker.id, date=day, close=D(close), high=D(close),
                             low=D(close), open=D(close), source="fake"))
    session.flush()
    return ticker


@pytest.fixture
def settings() -> Settings:
    return Settings(initial_cash=D("100000"), commission_per_share=D("0"),
                    min_commission=D("0"), slippage_bps=D("0"))


def test_session_date_rolls_weekends_back_to_friday():
    assert last_session_date(_at(dt.date(2026, 11, 14), 15)) == dt.date(2026, 11, 13)  # Sat
    assert last_session_date(_at(dt.date(2026, 11, 15), 15)) == dt.date(2026, 11, 13)  # Sun
    assert last_session_date(_at(TUE, 15)) == TUE


def test_utc_evening_is_still_the_same_new_york_day():
    # 23:30 UTC Tue = 18:30 ET Tue; 03:00 UTC Wed = 22:00 ET Tue.
    assert market_today(_at(TUE, 23, 30)) == TUE
    assert market_today(_at(TUE + dt.timedelta(days=1), 3)) == TUE


def test_fresh_price_never_calls_the_provider(session):
    ticker = _bar(session, "FRSH", TUE, "50")
    provider = FakeProvider()
    fresh = ensure_fresh_price(session, ticker, provider, now=_at(TUE, 20))
    assert (fresh.price, fresh.as_of, fresh.refreshed, fresh.stale_reason) == (
        D("50"), TUE, False, None)
    assert provider.calls == []


def test_stale_price_is_refreshed_from_a_live_quote(session, settings):
    ticker = _bar(session, "OLDP", dt.date(2026, 11, 3), "50")
    quote = Quote(symbol="OLDP", price=D("57.25"), as_of=_at(TUE, 18))
    fresh = ensure_fresh_price(session, ticker, FakeProvider(quote=quote), now=_at(TUE, 18))
    assert fresh.refreshed and fresh.stale_reason is None
    assert (fresh.price, fresh.as_of) == (D("57.25"), TUE)

    account = get_or_open_account(session, settings)
    result = execute_order(session, account, "OLDP", "buy", 1, settings=settings)
    assert result.accepted and result.order.fill_price == D("57.25")


def test_stale_price_without_a_quote_is_refused_with_its_age(session, settings):
    ticker = _bar(session, "DEAD", dt.date(2026, 11, 6), "50")
    fresh = ensure_fresh_price(session, ticker, FakeProvider(fail={"quote"}), now=_at(TUE, 18))
    assert fresh.stale_reason == ("price from 2026-11-06 is 4 days old and no live quote "
                                  "was available")
    account = get_or_open_account(session, settings)
    result = execute_order(session, account, "DEAD", "buy", 1, settings=settings,
                           price_not_before=fresh.required_since)
    assert not result.accepted and result.order.status == "rejected"
    assert "stale price" in result.reason and "2026-11-06" in result.reason
    assert account.cash_balance == D("100000")


def test_a_holiday_quote_dated_the_prior_session_is_accepted(session):
    """Thanksgiving: the market's latest price really is Wednesday's close."""
    wed, thu = dt.date(2026, 11, 25), dt.date(2026, 11, 26)
    ticker = _bar(session, "HOLI", wed, "80")
    quote = Quote(symbol="HOLI", price=D("80"), as_of=_at(wed, 21))
    fresh = ensure_fresh_price(session, ticker, FakeProvider(quote=quote), now=_at(thu, 16))
    assert fresh.stale_reason is None and fresh.refreshed
    assert fresh.as_of == wed


def test_provider_exceptions_surface_as_stale_not_crash(session):
    class Exploding(FakeProvider):
        def get_quote(self, symbol, *, asset_class="stock"):
            raise RuntimeError("network down")

    ticker = _bar(session, "BOOM", dt.date(2026, 11, 2), "10")
    fresh = ensure_fresh_price(session, ticker, Exploding(), now=_at(TUE, 18))
    assert fresh.stale_reason and "no live quote" in fresh.stale_reason
