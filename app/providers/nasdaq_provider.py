"""Nasdaq public API provider (no key required) - verified live on this host.

Endpoints: ``/api/quote/{sym}/chart`` (full daily OHLCV in ``data.chart[].z``,
~251 rows/yr), ``/info`` (name, last price, timeAsOf), ``/summary`` (sector,
industry, market cap, 52-week range, dividend, consensus 1-year target) and
``/api/calendar/earnings?date=`` (every company reporting that day, with EPS
forecast / actual / surprise % - one call covers the whole market).

Nasdaq encodes numbers as human strings ("$348.9607", "5,064,610,748,820",
"$355.38/$243.42") with "--" for missing; :mod:`app.ingestion.normalize` handles
that.  Rating history and news are not served by the endpoints available to us,
so those capabilities honestly report "unsupported".

Transport note: this host rejects plain httpx/requests with 403 "Access Denied"
(TLS fingerprinting), so requests go out through ``curl_cffi`` browser
impersonation - verified working here where curl also works and httpx does not.
"""

from __future__ import annotations

import datetime as dt

from curl_cffi import requests as browser_requests
from curl_cffi.requests.exceptions import RequestException

from app.config import Settings
from app.ingestion.normalize import (
    bar_from_nasdaq_z, clean_text, parse_datetime, percent_to_points, percent_to_ratio,
    ratio, split_pair, to_decimal,
)
from app.providers.base import (
    EarningsEvent, FundamentalsSnapshot, MarketDataProvider, ProviderResult, Quote, Side,
)
from app.providers.throttle import (
    HostThrottler, PermanentProviderError, TransientProviderError, call_with_retries,
)

_HOST = "api.nasdaq.com"
_BASE = f"https://{_HOST}/api"
_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nasdaq.com/",
    "Origin": "https://www.nasdaq.com",
}
#: Tried in order when the caller's asset class does not resolve.
_ASSET_ORDER: tuple[Side, ...] = ("stock", "etf", "index")
#: Our vocabulary -> Nasdaq's ``assetclass`` query values (note the plural).
_ASSET_PARAM = {"stock": "stocks", "etf": "etf", "index": "index"}


class NasdaqProvider(MarketDataProvider):
    """Key-free fallback for prices, quotes and headline fundamentals."""

    name = "nasdaq"

    def __init__(self, settings: Settings, throttler: HostThrottler | None = None) -> None:
        self.settings = settings
        self.throttler = throttler or HostThrottler(settings.min_provider_interval_seconds)
        # api.nasdaq.com answers 403 "Access Denied" to plain httpx/requests
        # from this host - it fingerprints TLS.  curl_cffi (already a yfinance
        # dependency) impersonates a real browser and is accepted.
        self._client = browser_requests.Session(
            headers=_HEADERS,
            timeout=settings.provider_timeout_seconds,
            impersonate="chrome",
            allow_redirects=True,
        )

    def close(self) -> None:
        """Release the underlying HTTP client."""
        self._client.close()

    def _get_json(self, path: str, params: dict[str, str]):
        """Throttled GET with backoff; returns the parsed ``data`` payload."""

        def attempt():
            self.throttler.wait(_HOST)
            try:
                response = self._client.get(f"{_BASE}{path}", params=params)
            except RequestException as exc:  # timeouts, resets, DNS failures
                raise TransientProviderError(f"transport error: {type(exc).__name__}") from exc
            if response.status_code == 429 or response.status_code >= 500:
                raise TransientProviderError(f"HTTP {response.status_code}")
            if response.status_code >= 400:
                # 403/404 normally mean our assetclass guess is wrong, so the
                # caller should try the next candidate instead of retrying.
                raise PermanentProviderError(f"HTTP {response.status_code}")
            try:
                payload = response.json()
            except Exception as exc:
                raise PermanentProviderError("response was not JSON") from exc
            if not isinstance(payload, dict):
                raise PermanentProviderError("non-JSON payload")
            data = payload.get("data")
            if data is None:
                status = payload.get("status") or {}
                raise PermanentProviderError(
                    f"no data ({status.get('message') or 'symbol unknown'})"
                )
            return data

        return call_with_retries(
            attempt,
            retries=self.settings.max_retries,
            base=self.settings.backoff_base_seconds,
            ceiling=self.settings.backoff_max_seconds,
        )

    def _get_with_asset_class(
        self, path: str, symbol: str, asset_class: Side, extra: dict[str, str]
    ):
        """Resolve ``assetclass`` (stocks/etf/index), then fetch once resolved."""
        tried = [asset_class] + [a for a in _ASSET_ORDER if a != asset_class]
        last_error: Exception | None = None
        for candidate in tried:
            try:
                params = {"assetclass": _ASSET_PARAM.get(candidate, "stocks"), **extra}
                return self._get_json(path, params), candidate
            except PermanentProviderError as exc:
                last_error = exc
        raise last_error or PermanentProviderError("unresolvable asset class")

    # -------------------------------------------------------- earnings calendar
    def get_earnings_calendar(self, day: dt.date) -> ProviderResult:
        """Market-wide reporters for ``day``.  EPS like ``"($1.10)"`` is
        negative; ``"N/A"``/blank stays unknown.  A row with an actual EPS is
        ``reported``, otherwise ``scheduled``."""
        endpoint = f"earnings_calendar({day.isoformat()})"
        try:
            data = self._get_json("/calendar/earnings", {"date": day.isoformat()})
        except (TransientProviderError, PermanentProviderError) as exc:
            return ProviderResult(self.name, endpoint, ok=False, error=str(exc))
        events = []
        for row in (data.get("rows") or []) if isinstance(data, dict) else []:
            symbol = clean_text(row.get("symbol"))
            if not symbol:
                continue
            actual = to_decimal(row.get("eps"))
            events.append(EarningsEvent(
                symbol=symbol.upper().replace("/", "-").replace(".", "-"),
                event_date=day,
                eps_estimate=to_decimal(row.get("epsForecast")),
                eps_actual=actual,
                surprise_pct=to_decimal(row.get("surprise")) if actual is not None else None,
                status="reported" if actual is not None else "scheduled",
                source=self.name,
            ))
        return ProviderResult(self.name, endpoint, ok=True, data=tuple(events),
                              row_count=len(events))

    # ----------------------------------------------------------------- history
    def get_history(
        self, symbol: str, start: dt.date, end: dt.date, *, asset_class: Side = "stock"
    ) -> ProviderResult:
        """Daily bars from the chart series (both endpoint dates inclusive)."""
        try:
            data, resolved = self._get_with_asset_class(
                f"/quote/{symbol}/chart", symbol, asset_class,
                {"fromdate": start.isoformat(), "todate": end.isoformat()},
            )
        except (TransientProviderError, PermanentProviderError) as exc:
            return ProviderResult(self.name, f"history({symbol})", ok=False, error=str(exc))

        bars = []
        for entry in (data.get("chart") or []):
            zone = entry.get("z") if isinstance(entry, dict) else None
            if not isinstance(zone, dict):
                continue
            bar = bar_from_nasdaq_z(symbol, zone, self.name)
            if bar is not None and start <= bar.date <= end:
                bars.append(bar)
        as_of = self._as_of_from(data, max((b.date for b in bars), default=None))
        return ProviderResult(
            provider=self.name, endpoint=f"history({symbol},asset={resolved})",
            ok=bool(bars), data=tuple(bars), row_count=len(bars), as_of=as_of,
            error=None if bars else "empty chart series",
        )

    @staticmethod
    def _as_of_from(data: dict, fallback_date: dt.date | None) -> dt.datetime | None:
        """Prefer the exchange's "timeAsOf" stamp, else the last bar's date."""
        stamp = parse_datetime(data.get("timeAsOf"))
        if stamp:
            return stamp
        if fallback_date:
            return dt.datetime.combine(fallback_date, dt.time(16, 0), dt.timezone.utc)
        return None

    # ------------------------------------------------------------------- quote
    def get_quote(self, symbol: str, *, asset_class: Side = "stock") -> ProviderResult:
        """Last sale price + ``lastTradeTimestamp`` from ``/info``."""
        try:
            data, resolved = self._get_with_asset_class(
                f"/quote/{symbol}/info", symbol, asset_class, {}
            )
        except (TransientProviderError, PermanentProviderError) as exc:
            return ProviderResult(self.name, f"quote({symbol})", ok=False, error=str(exc))

        primary = data.get("primaryData") or {}
        price = to_decimal(primary.get("lastSalePrice"))
        if price is None:
            return ProviderResult(self.name, f"quote({symbol})", ok=False, error="no price")
        as_of = parse_datetime(primary.get("lastTradeTimestamp")) or self._as_of_from(
            data, dt.date.today()
        )
        symbol_info = data.get("symbolInfo") or {}
        previous_close = to_decimal(data.get("previousClose"))
        market_cap_raw = data.get("marketCap")
        market_cap = to_decimal(
            market_cap_raw.get("value") if isinstance(market_cap_raw, dict) else market_cap_raw
        )
        quote = Quote(
            symbol=symbol.upper(), price=price, as_of=as_of, source=self.name,
            currency=clean_text(primary.get("currency"), 8),
            previous_close=previous_close, market_cap=market_cap,
            name=clean_text(data.get("company") or symbol_info.get("companyName"), 200),
        )
        missing = tuple(
            name for name, value in (("previous_close", previous_close), ("market_cap", market_cap))
            if value is None
        )
        return ProviderResult(
            provider=self.name, endpoint=f"quote({symbol},asset={resolved})", ok=True,
            data=(quote,), row_count=1, as_of=as_of, missing_fields=missing,
        )

    # ------------------------------------------------------------- fundamentals
    def get_fundamentals(self, symbol: str, *, asset_class: Side = "stock") -> ProviderResult:
        """Headline valuation from ``/summary`` (this feed lacks PE/EV-EBITDA)."""
        try:
            data, resolved = self._get_with_asset_class(
                f"/quote/{symbol}/summary", symbol, asset_class, {}
            )
        except (TransientProviderError, PermanentProviderError) as exc:
            return ProviderResult(self.name, f"fundamentals({symbol})", ok=False, error=str(exc))

        raw = data.get("summaryData") or {}

        def label(name: str) -> object:
            entry = raw.get(name) or {}
            return entry.get("value") if isinstance(entry, dict) else None

        high, low = split_pair(label("FiftTwoWeekHighLow"))
        snapshot = FundamentalsSnapshot(
            symbol=symbol.upper(),
            as_of_date=dt.date.today(),
            source=self.name,
            name=clean_text(data.get("company"), 200),
            sector=clean_text(label("Sector"), 100),
            industry=clean_text(label("Industry"), 120),
            market_cap=to_decimal(label("MarketCap")),
            pe_trailing=ratio(label("PeRatio")),
            pe_forward=ratio(label("ForwardPeRatio")),
            ps=ratio(label("PsRatio")),
            pb=ratio(label("PtbookRatio")),
            # Nasdaq prints both as percentages ("105.58%"): ROE is stored as
            # percent points (105.58), D/E as a plain ratio (1.0558).
            debt_to_equity=percent_to_ratio(label("DebtEquityRatio")),
            return_on_equity=percent_to_points(
                label("ReturnOnAverageEquity"), source_is_fraction=False
            ),
            dividend_yield=percent_to_points(label("Yield"), source_is_fraction=False),
            current_price=to_decimal(label("PreviousClose")),
            target_price_consensus=ratio(label("OneYrTarget")),
            fifty_two_week_high=high,
            fifty_two_week_low=low,
        )
        missing = tuple(
            name for name in (
                "pe_trailing", "pe_forward", "ev_ebitda", "ps", "pb", "debt_to_equity",
                "revenue_growth_yoy", "eps_growth_yoy", "fcf_yield", "return_on_equity",
                "short_pct_float", "target_price_consensus",
            )
            if getattr(snapshot, name, None) is None
        )
        return ProviderResult(
            provider=self.name, endpoint=f"fundamentals({symbol},asset={resolved})", ok=True,
            data=(snapshot,), row_count=1, as_of=dt.datetime.now(dt.timezone.utc),
            missing_fields=missing,
        )


