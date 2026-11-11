"""Never fill a paper order at an old price.

A market order may only fill at a price from the latest US trading session.
When the stored price is older (no refresh since yesterday, or the app was
off for a week), a live quote is fetched first.  If that fails the order is
refused - with the price's age - instead of filling at a stale close, which
is the single biggest way a paper fill can differ from a real one.

Trading days: weekdays in America/New_York.  There is deliberately no holiday
calendar: a *successful* live quote counts as current whatever its date, so on
a market holiday the quote simply carries the last session's price.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.db.models import Ticker
from app.providers.base import MarketDataProvider
from app.trading.orders import latest_price

MARKET_TZ = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class FreshPrice:
    """The price an order would fill at, and how it was established."""

    price: Decimal | None
    as_of: dt.date | None
    refreshed: bool             # True = priced from a live quote just now
    stale_reason: str | None    # set = do not fill; pass `price_not_before`
    required_since: dt.date     # the session date a fill price must reach

    def describe(self) -> str:
        if self.price is None:
            return "unpriced"
        how = "live quote" if self.refreshed else "stored price"
        return f"{how} as of {self.as_of}"


def market_today(now: dt.datetime | None = None) -> dt.date:
    """Today's date on the exchange clock (UTC evening is still 'today' in NY)."""
    now = now or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    return now.astimezone(MARKET_TZ).date()


def last_session_date(now: dt.datetime | None = None) -> dt.date:
    """Latest weekday on or before today, New York time."""
    day = market_today(now)
    while day.weekday() >= 5:  # Sat/Sun roll back to Friday
        day -= dt.timedelta(days=1)
    return day


def ensure_fresh_price(
    session: Session, ticker: Ticker, provider: MarketDataProvider | None,
    *, now: dt.datetime | None = None,
) -> FreshPrice:
    """Return a fill-worthy price for ``ticker``, refreshing the quote if needed."""
    from app.ingestion.ingest import ingest_quote  # ingestion imports trading; keep lazy

    required = last_session_date(now)
    priced = latest_price(session, ticker.id)
    if priced is not None and priced[1] >= required:
        return FreshPrice(priced[0], priced[1], False, None, required)

    quote = None
    if provider is not None:
        try:
            _, quote = ingest_quote(session, provider, ticker.symbol,
                                    asset_class=ticker.asset_class or "stock")
        except Exception:  # provider trouble must surface as "stale", not a 500
            quote = None
    if quote is not None:
        session.flush()
        refreshed = latest_price(session, ticker.id)
        if refreshed is not None:
            return FreshPrice(refreshed[0], refreshed[1], True, None, required)

    if priced is None:
        reason = "no stored price and no live quote was available"
        return FreshPrice(None, None, False, reason, required)
    age = (market_today(now) - priced[1]).days
    reason = (f"price from {priced[1]} is {age} day{'s' if age != 1 else ''} old "
              f"and no live quote was available")
    return FreshPrice(priced[0], priced[1], False, reason, required)
