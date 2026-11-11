"""Fallback chain: who served the data, and what nobody could supply."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from app.providers.fallback import FallbackProvider
from app.providers.base import FundamentalsSnapshot
from tests.conftest import FakeProvider, make_bar


def _chain() -> FallbackProvider:
    primary = FakeProvider(
        name="yfinance", bars=[make_bar(close="100.0")],
        fail={"history", "fundamentals", "news"},
    )
    secondary = FakeProvider(name="nasdaq", bars=[make_bar(close="99.5")])
    tertiary = FakeProvider(name="fred")
    return FallbackProvider(providers=[primary, secondary, tertiary], name="auto")


def test_falls_through_to_the_next_provider_that_has_data():
    chain = _chain()
    result = chain.get_history("AAPL", dt.date(2026, 1, 1), dt.date(2026, 1, 31))
    assert result.ok
    assert result.provider == "nasdaq"
    assert result.data[0].close == Decimal("99.5")


def test_provenance_records_the_serving_provider():
    chain = _chain()
    chain.get_history("AAPL", dt.date(2026, 1, 1), dt.date(2026, 1, 31))
    provenance = chain.provenance_for("history", "AAPL")
    assert provenance is not None
    assert provenance.provider == "nasdaq"
    assert provenance.attempted == ("yfinance", "nasdaq")
    assert "served by nasdaq" in provenance.describe()


def test_every_provider_failing_reports_unavailable_instead_of_inventing_data():
    chain = _chain()
    result = chain.get_news("AAPL")
    assert not result.ok
    assert result.data == ()
    provenance = chain.provenance_for("news", "AAPL")
    assert provenance is not None
    assert provenance.provider is None
    assert provenance.describe().startswith("UNAVAILABLE")


def test_a_crashing_provider_does_not_sink_the_chain():
    class Exploding(FakeProvider):
        def get_history(self, symbol, start, end, *, asset_class="stock"):
            raise RuntimeError("socket exploded")

    chain = FallbackProvider(
        providers=[Exploding(name="broken"), FakeProvider(name="nasdaq", bars=[make_bar()])]
    )
    result = chain.get_history("AAPL", dt.date(2026, 1, 1), dt.date(2026, 1, 31))
    assert result.ok and result.provider == "nasdaq"
    errors = dict(chain.provenance_for("history", "AAPL").errors)
    assert "socket exploded" in errors["broken"]


def test_missing_fields_survive_the_chain_into_provenance():
    snapshot = FundamentalsSnapshot(
        symbol="MSFT", as_of_date=dt.date(2026, 11, 10), source="nasdaq",
        pe_trailing=None, target_price_consensus=Decimal("630"),
    )
    chain = FallbackProvider(providers=[
        FakeProvider(name="yfinance", fail={"fundamentals"}),
        FakeProvider(name="nasdaq", fundamentals=snapshot, missing_fields=("pe_trailing",)),
    ])
    result = chain.get_fundamentals("MSFT")
    assert result.ok
    provenance = chain.provenance_for("fundamentals", "MSFT")
    assert provenance is not None
    assert "pe_trailing" in provenance.missing_fields
    assert "unavailable: pe_trailing" in provenance.describe()


def test_unsupported_capability_is_skipped_not_retried():
    """A provider that never implements a capability must not be blamed."""

    class HistoryOnly(FakeProvider):
        def get_news(self, symbol, *, asset_class="stock"):
            from app.providers.base import ProviderResult

            return ProviderResult(self.name, "news", ok=False, error="unsupported")

    chain = FallbackProvider(providers=[HistoryOnly(name="fred")])
    result = chain.get_news("AAPL")
    assert not result.ok
    assert chain.provenance_for("news", "AAPL").errors == (("fred", "unsupported"),)
