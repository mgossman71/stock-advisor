"""Market-data provider package.

Public entry point is :func:`app.providers.registry.build_provider`, which
returns either a single provider or a :class:`FallbackProvider` chain.
"""

from app.providers.base import (
    AnalystAction, Bar, EarningsEvent, FundamentalsSnapshot, MarketDataProvider,
    NewsHeadline, ProviderResult, Quote, UniverseMember,
)
from app.providers.fallback import FallbackProvider, Provenance
from app.providers.registry import build_provider, provider_names

__all__ = [
    "AnalystAction", "Bar", "EarningsEvent", "FallbackProvider", "FundamentalsSnapshot",
    "MarketDataProvider", "NewsHeadline", "Provenance", "ProviderResult", "Quote",
    "UniverseMember", "build_provider", "provider_names",
]
