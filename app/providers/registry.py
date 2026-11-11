"""Build the configured provider stack.

``MARKET_DATA_PROVIDER=auto`` (default) yields yfinance -> nasdaq -> fred.
Pin a single name to debug one source in isolation.  Keyed providers (Finnhub,
Alpha Vantage, Polygon) slot into the same chain the moment a key is present in
``.env`` - business logic never changes.
"""

from __future__ import annotations

from app.config import Settings, get_settings
from app.providers.base import MarketDataProvider
from app.providers.fallback import FallbackProvider
from app.providers.fred_provider import FredProvider
from app.providers.nasdaq_provider import NasdaqProvider
from app.providers.throttle import HostThrottler
from app.providers.yfinance_provider import YFinanceProvider


def build_provider(settings: Settings | None = None) -> MarketDataProvider:
    """Return the provider stack for the current settings."""
    settings = settings or get_settings()
    # One shared throttler: its gate is per-host, so providers never stampede a
    # remote endpoint even when the scheduler and a web request overlap.
    throttler = HostThrottler(settings.min_provider_interval_seconds)

    choice = (settings.market_data_provider or "auto").strip().lower()

    def build(name: str) -> MarketDataProvider | None:
        if name == "yfinance":
            return YFinanceProvider(settings, throttler)
        if name == "nasdaq":
            return NasdaqProvider(settings, throttler)
        if name == "fred":
            return FredProvider(settings, throttler)
        return None

    if choice != "auto":
        provider = build(choice)
        if provider is None:
            raise ValueError(
                f"unknown MARKET_DATA_PROVIDER={choice!r} (auto|yfinance|nasdaq|fred)"
            )
        return provider

    return FallbackProvider(
        providers=[p for p in (build(n) for n in ("yfinance", "nasdaq", "fred")) if p],
        name="auto",
    )


def provider_names(provider: MarketDataProvider) -> list[str]:
    """Names of the providers in play (useful in the UI header and CLI)."""
    if isinstance(provider, FallbackProvider):
        return [p.name for p in provider.providers]
    return [provider.name]
