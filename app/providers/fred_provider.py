"""FRED provider - key-free benchmark series (S&P 500, VIX).

Keeps benchmarking and the regime panel alive when Yahoo is down.  FRED serves
close-only daily observations, so bars carry ``close`` only and open/high/low
stay ``None`` rather than being faked from the close.
"""

from __future__ import annotations

import csv
import datetime as dt
import io

import httpx

from app.config import Settings
from app.ingestion.normalize import to_decimal
from app.providers.base import Bar, MarketDataProvider, ProviderResult, Quote, Side
from app.providers.throttle import (
    HostThrottler, PermanentProviderError, TransientProviderError, call_with_retries,
)

_HOST = "fred.stlouisfed.org"
_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
#: Yahoo-style symbols mapped to FRED series identifiers.
SERIES_BY_SYMBOL = {
    "^GSPC": "SP500",
    "^SPX": "SP500",
    "SPY": "SP500",
    "^VIX": "VIXCLS",
    "VIX": "VIXCLS",
}


class FredProvider(MarketDataProvider):
    """Daily observations for benchmark series via FRED's CSV endpoint."""

    name = "fred"

    def __init__(self, settings: Settings, throttler: HostThrottler | None = None) -> None:
        self.settings = settings
        self.throttler = throttler or HostThrottler(settings.min_provider_interval_seconds)
        self._client = httpx.Client(timeout=settings.provider_timeout_seconds)

    def close(self) -> None:
        """Release the HTTP client."""
        self._client.close()

    @staticmethod
    def series_for(symbol: str) -> str | None:
        """FRED series id for ``symbol``, or None when we have no mapping."""
        return SERIES_BY_SYMBOL.get(symbol.upper())

    def _fetch_rows(self, series: str, start: dt.date):
        def attempt():
            self.throttler.wait(_HOST)
            response = self._client.get(_CSV_URL, params={"id": series, "cosd": start.isoformat()})
            if response.status_code == 429 or response.status_code >= 500:
                raise TransientProviderError(f"HTTP {response.status_code}")
            if response.status_code == 404:
                raise PermanentProviderError(f"unknown series {series}")
            response.raise_for_status()
            reader = csv.DictReader(io.StringIO(response.text))
            if not reader.fieldnames or len(reader.fieldnames) < 2:
                raise PermanentProviderError(f"unexpected CSV shape for {series}")
            return reader.fieldnames[1], list(reader)

        return call_with_retries(
            attempt,
            retries=self.settings.max_retries,
            base=self.settings.backoff_base_seconds,
            ceiling=self.settings.backoff_max_seconds,
        )

    def get_history(
        self, symbol: str, start: dt.date, end: dt.date, *, asset_class: Side = "index"
    ) -> ProviderResult:
        """Close-only daily bars for the mapped FRED series."""
        series = self.series_for(symbol)
        if series is None:
            return ProviderResult(
                self.name, f"history({symbol})", ok=False, error=f"no FRED series for {symbol}"
            )
        try:
            column, rows = self._fetch_rows(series, start)
        except (TransientProviderError, PermanentProviderError) as exc:
            return ProviderResult(self.name, f"history({symbol})", ok=False, error=str(exc))

        bars: list[Bar] = []
        for row in rows:
            close = to_decimal(row.get(column))
            date = dt.date.fromisoformat(row["observation_date"]) if row.get("observation_date") else None
            if close is None or date is None or not (start <= date <= end):
                continue  # FRED leaves gaps as empty cells; we simply skip them
            bars.append(Bar(symbol=symbol.upper(), date=date, close=close, source=self.name))
        last = max((b.date for b in bars), default=None)
        return ProviderResult(
            provider=self.name, endpoint=f"history({symbol},series={series})",
            ok=bool(bars), data=tuple(bars), row_count=len(bars),
            as_of=dt.datetime.combine(last, dt.time(21, 0), dt.timezone.utc) if last else None,
            error=None if bars else "empty series",
        )

    def get_quote(self, symbol: str, *, asset_class: Side = "index") -> ProviderResult:
        """Latest observation of the mapped series."""
        result = self.get_history(
            symbol, dt.date.today() - dt.timedelta(days=30), dt.date.today()
        )
        if not result.ok:
            return ProviderResult(self.name, f"quote({symbol})", ok=False, error=result.error)
        latest = max(result.data, key=lambda b: b.date)
        return ProviderResult(
            provider=self.name, endpoint=f"quote({symbol})", ok=True,
            data=(Quote(
                symbol=symbol.upper(), price=latest.close,
                as_of=result.as_of or dt.datetime.now(dt.timezone.utc), source=self.name,
            ),),
            row_count=1, as_of=result.as_of,
        )
