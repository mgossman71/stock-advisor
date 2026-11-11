"""yfinance provider - the zero-API-key default (verified against yfinance 1.8.0).

Failures map to :class:`TransientProviderError` (rate limit / timeout) or
:class:`PermanentProviderError` (unknown symbol) so the retry wrapper and the
fallback chain behave correctly.  Every call passes through a shared
:class:`HostThrottler` before touching the network.
"""

from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal

import yfinance

from app.config import Settings
from app.ingestion.normalize import clean_text, parse_date, parse_datetime, percent_to_points, ratio, to_decimal, to_int
from app.providers.base import (
    AnalystAction, Bar, EarningsEvent, FundamentalsSnapshot, MarketDataProvider,
    NewsHeadline, ProviderResult, Quote, Side,
)
from app.providers.throttle import (
    HostThrottler, PermanentProviderError, TransientProviderError, call_with_retries,
)

_HOST = "query1.finance.yahoo.com"
_RATE_HINT = re.compile(r"(429|rate limit|too many requests|timeout|timed out|connection)", re.I)


class YFinanceProvider(MarketDataProvider):
    """Every capability through Yahoo's unofficial endpoints (no key needed)."""

    name = "yfinance"

    def __init__(self, settings: Settings, throttler: HostThrottler | None = None) -> None:
        self.settings = settings
        self.throttler = throttler or HostThrottler(settings.min_provider_interval_seconds)

    def _run(self, endpoint: str, fn):
        """Throttle, then run ``fn`` with backoff, classifying exceptions."""

        def attempt():
            self.throttler.wait(_HOST)
            return fn()

        try:
            return call_with_retries(
                attempt,
                retries=self.settings.max_retries,
                base=self.settings.backoff_base_seconds,
                ceiling=self.settings.backoff_max_seconds,
            )
        except (TransientProviderError, PermanentProviderError):
            raise
        except Exception as exc:  # yfinance raises many shapes; classify them
            message = f"{type(exc).__name__}: {exc}"
            raise (
                TransientProviderError(message)
                if _RATE_HINT.search(message)
                else PermanentProviderError(message)
            ) from exc

    def _ticker(self, symbol: str) -> yfinance.Ticker:
        return yfinance.Ticker(symbol)

    def get_history(
        self, symbol: str, start: dt.date, end: dt.date, *, asset_class: Side = "stock"
    ) -> ProviderResult:
        """Daily bars, inclusive of both endpoints (yfinance's end is exclusive)."""

        def fetch():
            return self._ticker(symbol).history(
                start=start, end=end + dt.timedelta(days=1), auto_adjust=False, actions=False
            )

        frame = self._run(f"history({symbol})", fetch)
        if frame is None or len(frame) == 0:
            return ProviderResult(self.name, f"history({symbol})", ok=False, error="empty")

        bars: list[Bar] = []
        for index, row in frame.iterrows():
            close = to_decimal(row.get("Close"))
            date = parse_date(index)
            if close is None or date is None:
                continue  # no close -> no bar; never store a price we cannot price
            bars.append(
                Bar(
                    symbol=symbol.upper(),
                    date=date,
                    close=close,
                    open=to_decimal(row.get("Open")),
                    high=to_decimal(row.get("High")),
                    low=to_decimal(row.get("Low")),
                    adj_close=to_decimal(row.get("Adj Close")),
                    volume=to_int(row.get("Volume")),
                    source=self.name,
                )
            )
        as_of_date = max((b.date for b in bars), default=None)
        return ProviderResult(
            provider=self.name,
            endpoint=f"history({symbol})",
            ok=bool(bars),
            data=tuple(bars),
            row_count=len(bars),
            as_of=(
                dt.datetime.combine(as_of_date, dt.time(16, 0), dt.timezone.utc)
                if as_of_date else None
            ),
            error=None if bars else "no usable rows",
        )

    def get_quote(self, symbol: str, *, asset_class: Side = "stock") -> ProviderResult:
        """Latest price from ``fast_info`` plus the best available as-of stamp."""

        def fetch():
            ticker = self._ticker(symbol)
            return ticker.fast_info, ticker.history(period="5d", auto_adjust=True, actions=False)

        fast_info, frame = self._run(f"quote({symbol})", fetch)
        price = to_decimal(getattr(fast_info, "last_price", None))
        if price is None and frame is not None and len(frame):
            price = to_decimal(frame["Close"].iloc[-1])
        if price is None:
            return ProviderResult(self.name, f"quote({symbol})", ok=False, error="no price")

        previous_close = to_decimal(getattr(fast_info, "previous_close", None))
        market_cap = to_decimal(getattr(fast_info, "market_cap", None))
        as_of = self._quote_timestamp(fast_info, frame)
        quote = Quote(
            symbol=symbol.upper(), price=price, as_of=as_of, source=self.name,
            currency=getattr(fast_info, "currency", None),
            previous_close=previous_close, market_cap=market_cap,
        )
        missing = tuple(
            name for name, value in (("previous_close", previous_close), ("market_cap", market_cap))
            if value is None
        )
        return ProviderResult(
            provider=self.name, endpoint=f"quote({symbol})", ok=True,
            data=(quote,), row_count=1, as_of=as_of, missing_fields=missing,
        )

    @staticmethod
    def _quote_timestamp(fast_info, frame) -> dt.datetime:
        """Exchange timestamp if offered, else the last bar's date at 16:00 UTC."""
        for attr in ("last_market_time", "last_time"):
            raw = getattr(fast_info, attr, None)
            if raw is not None:
                parsed = parse_datetime(raw)
                if parsed:
                    return parsed
        if frame is not None and len(frame):
            last_bar = parse_datetime(frame.index[-1])
            if last_bar:
                return last_bar.replace(hour=16, minute=0, second=0, microsecond=0)
        return dt.datetime.now(dt.timezone.utc)

    # ------------------------------------------------------------- fundamentals
    def get_fundamentals(self, symbol: str, *, asset_class: Side = "stock") -> ProviderResult:
        """``info`` keys + ``valuation`` history + consensus price target."""

        def fetch():
            ticker = self._ticker(symbol)
            info = ticker.info or {}
            try:
                valuation = ticker.valuation
            except Exception:
                valuation = None
            try:
                targets = ticker.analyst_price_targets or {}
            except Exception:
                targets = {}
            return info, valuation, targets

        info, valuation, targets = self._run(f"fundamentals({symbol})", fetch)
        if not info:
            return ProviderResult(
                self.name, f"fundamentals({symbol})", ok=False, error="empty info"
            )

        market_cap = to_decimal(info.get("marketCap"))
        free_cash = to_decimal(info.get("freeCashflow"))
        # fcf_yield is a documented derivation from two provider numbers
        # (freeCashflow / marketCap), not an interpolation.
        fcf_yield = (
            (free_cash / market_cap * 100)
            if free_cash is not None and market_cap not in (None, 0) else None
        )
        dividend_points = percent_to_points(
            info.get("trailingAnnualDividendYield"), source_is_fraction=True
        )
        if dividend_points is None:
            dividend_points = percent_to_points(info.get("dividendYield"), source_is_fraction=False)
        debt_to_equity = to_decimal(info.get("debtToEquity"))

        snapshot = FundamentalsSnapshot(
            symbol=symbol.upper(),
            as_of_date=dt.date.today(),
            source=self.name,
            name=clean_text(info.get("longName") or info.get("shortName"), max_length=200),
            sector=clean_text(info.get("sector"), max_length=100),
            industry=clean_text(info.get("industry"), max_length=120),
            currency=clean_text(info.get("currency"), max_length=8),
            market_cap=market_cap,
            pe_trailing=ratio(info.get("trailingPE")),
            pe_forward=ratio(info.get("forwardPE")),
            ev_ebitda=ratio(info.get("enterpriseToEbitda")),
            ps=ratio(info.get("priceToSalesTrailing12Months")),
            pb=ratio(info.get("priceToBook")),
            # Yahoo reports debtToEquity in percent (84.286 == 84 %); store a ratio.
            debt_to_equity=(debt_to_equity / 100) if debt_to_equity is not None else None,
            revenue_growth_yoy=percent_to_points(info.get("revenueGrowth"), source_is_fraction=True),
            eps_growth_yoy=percent_to_points(info.get("earningsGrowth"), source_is_fraction=True),
            dividend_yield=dividend_points,
            fcf_yield=fcf_yield,
            gross_margin=percent_to_points(info.get("grossMargins"), source_is_fraction=True),
            operating_margin=percent_to_points(info.get("operatingMargins"), source_is_fraction=True),
            profit_margin=percent_to_points(info.get("profitMargins"), source_is_fraction=True),
            return_on_equity=percent_to_points(info.get("returnOnEquity"), source_is_fraction=True),
            current_price=ratio(info.get("currentPrice")),
            target_price_consensus=ratio(info.get("targetMeanPrice") or targets.get("mean")),
            short_pct_float=percent_to_points(info.get("shortPercentOfFloat"), source_is_fraction=True),
            beta=ratio(info.get("beta")),
            fifty_two_week_high=ratio(info.get("fiftyTwoWeekHigh")),
            fifty_two_week_low=ratio(info.get("fiftyTwoWeekLow")),
            pe_history=_pe_history(valuation),
        )
        missing = tuple(n for n in _FUNDAMENTAL_FIELDS if getattr(snapshot, n, None) is None)
        return ProviderResult(
            provider=self.name, endpoint=f"fundamentals({symbol})", ok=True,
            data=(snapshot,), row_count=1, as_of=dt.datetime.now(dt.timezone.utc),
            missing_fields=missing,
        )

    # -------------------------------------------------------------------- news
    def get_news(self, symbol: str, *, asset_class: Side = "stock") -> ProviderResult:
        """Recent headlines.  Sentiment stays None - Yahoo supplies none."""

        def fetch():
            return self._ticker(symbol).get_news() or []

        raw = self._run(f"news({symbol})", fetch)
        headlines: list[NewsHeadline] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if isinstance(content, dict):  # yfinance 1.x shape
                title = clean_text(content.get("title"), max_length=400)
                url = (content.get("canonicalUrl") or {}).get("url") or (
                    content.get("clickThroughUrl") or {}
                ).get("url")
                published = parse_datetime(content.get("pubDate") or content.get("displayTime"))
                source = clean_text((content.get("provider") or {}).get("displayName"), 120)
                excerpt = clean_text(
                    content.get("summary") or content.get("description"), max_length=600
                )
            else:  # legacy flat shape
                title = clean_text(item.get("title"), max_length=400)
                url = item.get("link") or item.get("url")
                published = parse_datetime(item.get("providerPublishTime"))
                source = clean_text(item.get("publisher"), 120)
                excerpt = clean_text(item.get("summary"), max_length=600)
            if not title or not url:
                continue
            headlines.append(NewsHeadline(
                symbol=symbol.upper(), title=title, url=str(url), published_at=published,
                source=source, body_excerpt=excerpt, sentiment_score=None, provider=self.name,
            ))
        return ProviderResult(
            provider=self.name, endpoint=f"news({symbol})", ok=bool(headlines),
            data=tuple(headlines), row_count=len(headlines),
            as_of=dt.datetime.now(dt.timezone.utc),
            error=None if headlines else "no headlines",
        )

    # --------------------------------------------------------- analyst actions
    def get_analyst_actions(self, symbol: str, *, asset_class: Side = "stock") -> ProviderResult:
        """Rating changes + price-target moves from ``upgrades_downgrades``."""

        def fetch():
            return self._ticker(symbol).upgrades_downgrades

        frame = self._run(f"analyst_actions({symbol})", fetch)
        if frame is None or not len(frame):
            return ProviderResult(
                self.name, f"analyst_actions({symbol})", ok=False, error="empty"
            )

        actions: list[AnalystAction] = []
        for index, row in frame.iterrows():
            when = parse_date(index)
            if when is None:
                continue
            action = _classify_action(row)
            if action is None:
                continue
            actions.append(AnalystAction(
                symbol=symbol.upper(), action_date=when, action=action,
                firm=clean_text(row.get("Firm"), 120),
                old_rating=clean_text(row.get("FromGrade"), 60),
                new_rating=clean_text(row.get("ToGrade"), 60),
                old_target=ratio(row.get("priorPriceTarget")),
                new_target=ratio(row.get("currentPriceTarget")),
                source=self.name,
            ))
        return ProviderResult(
            provider=self.name, endpoint=f"analyst_actions({symbol})", ok=bool(actions),
            data=tuple(actions), row_count=len(actions),
            as_of=dt.datetime.now(dt.timezone.utc), error=None if actions else "no actions",
        )

    # -------------------------------------------------------------- earnings
    def get_earnings(self, symbol: str, *, asset_class: Side = "stock") -> ProviderResult:
        """Earnings dates with estimate / actual / surprise where reported."""

        def fetch():
            return self._ticker(symbol).earnings_dates

        frame = self._run(f"earnings({symbol})", fetch)
        if frame is None or not len(frame):
            return ProviderResult(self.name, f"earnings({symbol})", ok=False, error="empty")

        events: list[EarningsEvent] = []
        today = dt.date.today()
        for index, row in frame.iterrows():
            when = parse_date(index)
            if when is None:
                continue
            actual = to_decimal(row.get("Reported EPS"))
            events.append(EarningsEvent(
                symbol=symbol.upper(), event_date=when,
                eps_estimate=to_decimal(row.get("EPS Estimate")),
                eps_actual=actual,
                surprise_pct=to_decimal(row.get("Surprise(%)")),
                status="reported" if actual is not None else "scheduled",
                source=self.name,
            ))
        if not events:
            return ProviderResult(self.name, f"earnings({symbol})", ok=False, error="no rows")
        upcoming = min(
            (e for e in events if e.event_date >= today), key=lambda e: e.event_date, default=None
        )
        return ProviderResult(
            provider=self.name, endpoint=f"earnings({symbol})", ok=True,
            data=tuple(events), row_count=len(events),
            as_of=None,
        )


# ------------------------------------------------------------------ module utils
_FUNDAMENTAL_FIELDS = (
    "pe_trailing", "pe_forward", "ev_ebitda", "ps", "pb", "debt_to_equity",
    "revenue_growth_yoy", "eps_growth_yoy", "dividend_yield", "fcf_yield",
    "return_on_equity", "target_price_consensus", "short_pct_float", "beta",
    "fifty_two_week_high", "fifty_two_week_low", "market_cap", "sector", "industry",
)

#: Rough ordering of sell-side rating labels, weakest -> strongest.  Lets us
#: tell an upgrade from a downgrade when yfinance reports action="main".
_RATING_ORDER = [
    "strong sell", "sell", "underweight", "underperform", "sector underperform",
    "hold", "neutral", "sector perform", "sector in-line", "perform", "in-line",
    "overweight", "buy", "outperform", "sector outperform", "strong buy",
]


def _rank_rating(label: object) -> int | None:
    """Ordinal rank of a rating label, or ``None`` when unrecognised."""
    if not label:
        return None
    text = str(label).strip().lower()
    if text in _RATING_ORDER:
        return _RATING_ORDER.index(text)
    for i, known in enumerate(_RATING_ORDER):
        if text.startswith(known) or known.startswith(text):
            return i
    return None


def _classify_action(row) -> str | None:
    """Map yfinance's action/grade columns onto our action vocabulary."""
    raw_action = str(row.get("Action") or "").strip().lower()
    target_action = str(row.get("priceTargetAction") or "").strip().lower()
    old_rank = _rank_rating(row.get("FromGrade"))
    new_rank = _rank_rating(row.get("ToGrade"))

    if raw_action == "up":
        return "upgrade"
    if raw_action == "down":
        return "downgrade"
    if raw_action == "init":
        return "initiate"
    if old_rank is not None and new_rank is not None:
        if new_rank > old_rank:
            return "upgrade"
        if new_rank < old_rank:
            return "downgrade"
    if target_action.startswith("rais"):
        return "target_raise"
    if target_action.startswith(("lower", "cut")):
        return "target_cut"
    if raw_action in {"main", "reit"}:
        return "reassess"
    return None


def _pe_history(valuation) -> dict[str, Decimal]:
    """Trailing P/E by period from yfinance's ``valuation`` frame.

    This feeds the "vs. its own history" part of value scoring.  An empty dict
    means the provider gave us no history, and the report says exactly that.
    """
    history: dict[str, Decimal] = {}
    if valuation is None or not len(valuation):
        return history
    label = next(
        (name for name in valuation.index if str(name).strip().lower() == "trailing p/e"),
        None,
    )
    if label is None:
        return history
    for column in valuation.columns:
        value = to_decimal(valuation.loc[label, column])
        if value is not None:
            history[str(column)] = value
    return history




