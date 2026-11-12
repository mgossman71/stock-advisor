"""Market-data provider abstraction.

Every provider returns :class:`ProviderResult` wrappers around small frozen
dataclasses.  The contract that makes "never fabricate data" enforceable:

* A field the provider did not supply stays ``None``.  Providers must not
  substitute 0, a blank, a forward-fill or an interpolation.
* ``ProviderResult.ok`` is False when the call failed; ``data`` is then empty
  and ``error`` carries the reason for the ``data_fetch_log``.
* ``ProviderResult.provider`` + ``as_of`` travel with the data so the UI can
  show provenance and an as-of stamp on every price it renders.

Unit conventions (normalisation lives in :mod:`app.ingestion.normalize`):

* prices / per-share valuation -> ``Decimal`` in the instrument's currency
* percentages                  -> percent POINTS (``12.5`` means 12.5 %)
* multiples / ratios           -> unitless (P/E ``38.81``); ``debt_to_equity``
  is a ratio, so ``0.84`` means 84 % debt/equity
* volume                       -> ``int`` shares
"""

from __future__ import annotations

import datetime as dt
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal

Side = Literal["stock", "etf", "index"]


@dataclass(frozen=True)
class Bar:
    """One daily OHLCV bar."""

    symbol: str
    date: dt.date
    close: Decimal
    open: Decimal | None = None
    high: Decimal | None = None
    low: Decimal | None = None
    adj_close: Decimal | None = None
    volume: int | None = None
    source: str = "unknown"


@dataclass(frozen=True)
class Quote:
    """Latest price plus the timestamp the provider attached to it."""

    symbol: str
    price: Decimal
    as_of: dt.datetime
    source: str = "unknown"
    currency: str | None = None
    previous_close: Decimal | None = None
    market_cap: Decimal | None = None
    name: str | None = None


@dataclass(frozen=True)
class FundamentalsSnapshot:
    """Point-in-time fundamental/valuation snapshot (all fields optional)."""

    symbol: str
    as_of_date: dt.date
    source: str = "unknown"
    name: str | None = None
    sector: str | None = None
    industry: str | None = None
    currency: str | None = None
    market_cap: Decimal | None = None
    pe_trailing: Decimal | None = None
    pe_forward: Decimal | None = None
    ev_ebitda: Decimal | None = None
    ps: Decimal | None = None
    pb: Decimal | None = None
    debt_to_equity: Decimal | None = None
    revenue_growth_yoy: Decimal | None = None
    eps_growth_yoy: Decimal | None = None
    dividend_yield: Decimal | None = None
    fcf_yield: Decimal | None = None
    gross_margin: Decimal | None = None
    operating_margin: Decimal | None = None
    profit_margin: Decimal | None = None
    return_on_equity: Decimal | None = None
    current_price: Decimal | None = None
    target_price_consensus: Decimal | None = None
    short_pct_float: Decimal | None = None
    beta: Decimal | None = None
    fifty_two_week_high: Decimal | None = None
    fifty_two_week_low: Decimal | None = None
    #: Valuation multiples by period-end label, used for "vs. own 5-year PE"
    #: scoring.  Empty when the provider has no history - never back-filled.
    pe_history: dict[str, Decimal] = field(default_factory=dict)


@dataclass(frozen=True)
class NewsHeadline:
    """Headline + optional excerpt.  Sentiment stays None unless measured."""

    symbol: str
    title: str
    url: str
    published_at: dt.datetime | None = None
    source: str | None = None
    body_excerpt: str | None = None
    sentiment_score: Decimal | None = None
    provider: str = "unknown"


@dataclass(frozen=True)
class AnalystAction:
    """One firm's rating / price-target action."""

    symbol: str
    action_date: dt.date
    action: str  # upgrade | downgrade | target_raise | target_cut | initiate | reassess
    firm: str | None = None
    old_rating: str | None = None
    new_rating: str | None = None
    old_target: Decimal | None = None
    new_target: Decimal | None = None
    source: str = "unknown"


@dataclass(frozen=True)
class EarningsEvent:
    """Reported or upcoming earnings date."""

    symbol: str
    event_date: dt.date
    eps_estimate: Decimal | None = None
    revenue_estimate: Decimal | None = None
    eps_actual: Decimal | None = None
    revenue_actual: Decimal | None = None
    surprise_pct: Decimal | None = None
    status: str = "scheduled"  # reported | scheduled
    source: str = "unknown"


@dataclass(frozen=True)
class Dividend:
    """A cash dividend per share, keyed by its ex-dividend date.

    Amounts are as the provider serves them: split-adjusted to the current
    share basis (``app/ingestion/splits.py`` keeps stored rows in step).
    """

    symbol: str
    ex_date: dt.date
    amount: Decimal
    source: str = "unknown"


@dataclass(frozen=True)
class UniverseMember:
    """A symbol belonging to a screening universe (US-listed scan, S&P 500)."""

    symbol: str
    name: str | None = None
    sector: str | None = None
    industry: str | None = None
    market_cap: Decimal | None = None


@dataclass(frozen=True)
class ProviderResult:
    """Uniform envelope for every provider call."""

    provider: str
    endpoint: str
    ok: bool
    data: tuple[Any, ...] = ()
    error: str | None = None
    #: Fields the provider explicitly did not supply (rendered as "unavailable").
    missing_fields: tuple[str, ...] = ()
    as_of: dt.datetime | None = None
    row_count: int = 0

    @property
    def failed(self) -> bool:
        """True when the call errored (as opposed to simply being empty)."""
        return not self.ok

    def summary(self) -> str:
        """One-line human summary used in logs / CLI output."""
        if not self.ok:
            return f"{self.provider}:{self.endpoint} FAILED ({self.error or 'unknown'})"
        return f"{self.provider}:{self.endpoint} -> {self.row_count} rows"


class MarketDataProvider(ABC):
    """Abstract market-data source.

    Implementations must return ``ok=False`` - never synthetic data - when a
    capability is unsupported or the remote call fails, so the fallback chain
    and the reporting layer can both react honestly.
    """

    #: Short stable identifier stored in ``data_fetch_log.provider``.
    name: str = "abstract"

    @abstractmethod
    def get_history(
        self, symbol: str, start: dt.date, end: dt.date, *, asset_class: Side = "stock"
    ) -> ProviderResult:
        """Daily bars for ``[start, end]`` inclusive."""

    @abstractmethod
    def get_quote(self, symbol: str, *, asset_class: Side = "stock") -> ProviderResult:
        """Latest available price + as-of timestamp."""

    def get_quotes(self, symbols: list[str]) -> ProviderResult:
        """Live quotes for many symbols in one request (the bulk "Refresh
        prices").  ``data`` holds a :class:`Quote` per symbol the provider
        priced; missing symbols are simply absent.  Unsupported by default."""
        return ProviderResult(self.name, f"quotes({len(symbols)})", ok=False,
                              error="unsupported")

    def get_fundamentals(self, symbol: str, *, asset_class: Side = "stock") -> ProviderResult:
        """Fundamental/valuation snapshot.  Unsupported by default."""
        return ProviderResult(self.name, "fundamentals", ok=False, error="unsupported")

    def get_news(self, symbol: str, *, asset_class: Side = "stock") -> ProviderResult:
        """Recent headlines.  Unsupported by default."""
        return ProviderResult(self.name, "news", ok=False, error="unsupported")

    def get_analyst_actions(self, symbol: str, *, asset_class: Side = "stock") -> ProviderResult:
        """Rating / price-target actions.  Unsupported by default."""
        return ProviderResult(self.name, "analyst_actions", ok=False, error="unsupported")

    def get_earnings(self, symbol: str, *, asset_class: Side = "stock") -> ProviderResult:
        """Earnings dates + surprises.  Unsupported by default."""
        return ProviderResult(self.name, "earnings", ok=False, error="unsupported")

    def get_dividends(
        self, symbol: str, start: dt.date, end: dt.date, *, asset_class: Side = "stock"
    ) -> ProviderResult:
        """Cash dividends with ex-dates in ``[start, end]``.  ``ok=True`` with no
        rows means "pays nothing in range".  Unsupported by default."""
        return ProviderResult(self.name, "dividends", ok=False, error="unsupported")

    def get_earnings_calendar(self, day: dt.date) -> ProviderResult:
        """Every company that reported (or will report) on ``day``, market-wide,
        as :class:`EarningsEvent` rows.  ``ok=True`` with no rows means nobody
        reported that day (weekends, holidays).  Unsupported by default."""
        return ProviderResult(self.name, f"earnings_calendar({day})", ok=False,
                              error="unsupported")

    def get_universe(self, kind: str = "sp500") -> ProviderResult:
        """Constituent list for a screening universe.  Unsupported by default."""
        return ProviderResult(self.name, f"universe:{kind}", ok=False, error="unsupported")

