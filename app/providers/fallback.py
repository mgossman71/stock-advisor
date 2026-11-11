"""Fallback chain: try providers in order, record who served what.

This is what makes the app resilient without ever inventing data: each
capability is attempted against ``yfinance`` first, then the key-free fallbacks,
and the result carries the serving provider.  Reports render ``provenance`` so
you always know whether a number came from Yahoo, Nasdaq or FRED - and the
``missing_fields`` list shows precisely what nobody could supply.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from app.providers.base import MarketDataProvider, ProviderResult, Side


@dataclass(frozen=True)
class Provenance:
    """Which provider answered a capability, and what is still missing."""

    capability: str
    symbol: str
    provider: str | None
    ok: bool
    attempted: tuple[str, ...] = ()
    errors: tuple[tuple[str, str], ...] = ()
    missing_fields: tuple[str, ...] = ()
    as_of: dt.datetime | None = None

    def describe(self) -> str:
        """Human-readable provenance line for reports and the UI."""
        if self.ok and self.provider:
            notes: list[str] = []
            if self.errors:
                notes.append("after " + "; ".join(f"{name} {err}" for name, err in self.errors))
            if self.missing_fields:
                notes.append("unavailable: " + ", ".join(self.missing_fields))
            tail = f" ({' - '.join(notes)})" if notes else ""
            return f"served by {self.provider}{tail}"
        tried = ", ".join(f"{name}: {err}" for name, err in self.errors) or "no provider configured"
        return f"UNAVAILABLE - {tried}"


@dataclass
class FallbackProvider(MarketDataProvider):
    """Composite provider that walks an ordered list of providers."""

    providers: list[MarketDataProvider]
    name: str = "fallback"
    #: capability|symbol -> Provenance, populated as calls resolve.
    provenance: dict[str, Provenance] = field(default_factory=dict)

    def _resolve(self, capability: str, symbol: str, invoke) -> ProviderResult:
        """Return the first successful result; else an aggregated failure."""
        attempted: list[str] = []
        errors: list[tuple[str, str]] = []
        for provider in self.providers:
            attempted.append(provider.name)
            try:
                result = invoke(provider)
            except NotImplementedError:
                errors.append((provider.name, "not implemented"))
                continue
            except Exception as exc:  # a broken provider must not sink the chain
                errors.append((provider.name, f"{type(exc).__name__}: {exc}"))
                continue
            # A quote and a dividend list may legitimately be empty (a stock
            # that pays nothing); anything else empty means "try the next".
            if result.ok and (result.data or capability in ("quote", "dividends")):
                # Failures along the way are kept: "served by nasdaq after
                # yfinance failed" is exactly what you want when a number looks
                # different from yesterday's.
                self.provenance[f"{capability}:{symbol}"] = Provenance(
                    capability=capability, symbol=symbol, provider=provider.name, ok=True,
                    attempted=tuple(attempted), errors=tuple(errors),
                    missing_fields=result.missing_fields, as_of=result.as_of,
                )
                return result
            errors.append((provider.name, result.error or "empty"))
        self.provenance[f"{capability}:{symbol}"] = Provenance(
            capability=capability, symbol=symbol, provider=None, ok=False,
            attempted=tuple(attempted), errors=tuple(errors),
        )
        return ProviderResult(
            provider=self.name, endpoint=f"{capability}({symbol})", ok=False,
            error="; ".join(f"{n}:{e}" for n, e in errors) or "no provider",
        )

    # The chain forwards each capability to the providers in order.
    def get_history(self, symbol: str, start: dt.date, end: dt.date, *,
                    asset_class: Side = "stock") -> ProviderResult:
        return self._resolve("history", symbol, lambda p: p.get_history(symbol, start, end, asset_class=asset_class))

    def get_quote(self, symbol: str, *, asset_class: Side = "stock") -> ProviderResult:
        return self._resolve("quote", symbol, lambda p: p.get_quote(symbol, asset_class=asset_class))

    def get_fundamentals(self, symbol: str, *, asset_class: Side = "stock") -> ProviderResult:
        return self._resolve("fundamentals", symbol, lambda p: p.get_fundamentals(symbol, asset_class=asset_class))

    def get_news(self, symbol: str, *, asset_class: Side = "stock") -> ProviderResult:
        return self._resolve("news", symbol, lambda p: p.get_news(symbol, asset_class=asset_class))

    def get_analyst_actions(self, symbol: str, *, asset_class: Side = "stock") -> ProviderResult:
        return self._resolve("analyst_actions", symbol, lambda p: p.get_analyst_actions(symbol, asset_class=asset_class))

    def get_earnings(self, symbol: str, *, asset_class: Side = "stock") -> ProviderResult:
        return self._resolve("earnings", symbol, lambda p: p.get_earnings(symbol, asset_class=asset_class))

    def get_dividends(self, symbol: str, start: dt.date, end: dt.date, *,
                      asset_class: Side = "stock") -> ProviderResult:
        return self._resolve("dividends", symbol, lambda p: p.get_dividends(symbol, start, end, asset_class=asset_class))

    def get_universe(self, kind: str = "sp500") -> ProviderResult:
        return self._resolve("universe", kind, lambda p: p.get_universe(kind))

    def provenance_for(self, capability: str, symbol: str) -> Provenance | None:
        """Provenance recorded for the most recent ``capability`` on ``symbol``."""
        return self.provenance.get(f"{capability}:{symbol}")

    def close(self) -> None:
        """Close any provider that owns network resources."""
        for provider in self.providers:
            closer = getattr(provider, "close", None)
            if callable(closer):
                closer()
