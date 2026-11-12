"""Shared fixtures: an isolated SQLite DB and a scriptable fake provider.

Tests never touch the network - the fake provider plays the role of yfinance so
ingestion, fallback and idempotency logic are exercised deterministically.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import Base
import app.db.models  # noqa: F401 - registers every table on Base.metadata
from app.providers.base import (
    AnalystAction, Bar, Dividend, EarningsEvent, FundamentalsSnapshot, MarketDataProvider,
    NewsHeadline, ProviderResult, Quote, UniverseMember,
)


@pytest.fixture
def db_file(tmp_path: Path) -> Path:
    """A throwaway SQLite file with the full schema created."""
    path = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{path}", future=True)
    Base.metadata.create_all(engine)
    engine.dispose()
    return path


@pytest.fixture
def session(db_file: Path) -> Iterator[Session]:
    """A session bound to the throwaway database, FK enforcement on."""
    engine = create_engine(f"sqlite:///{db_file}", future=True)

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_connection, _record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    factory = sessionmaker(bind=engine, future=True, expire_on_commit=False)
    session = factory()
    try:
        yield session
        session.commit()
    finally:
        session.close()
        engine.dispose()


#: Fields the ingestion layer reports as "unavailable" when a snapshot leaves
#: them None - mirrors ``YFinanceProvider._FUNDAMENTAL_FIELDS``.
_FUNDAMENTAL_FIELDS = (
    "pe_trailing", "pe_forward", "ev_ebitda", "ps", "pb", "debt_to_equity",
    "revenue_growth_yoy", "eps_growth_yoy", "dividend_yield", "fcf_yield",
    "return_on_equity", "target_price_consensus", "short_pct_float", "beta",
    "fifty_two_week_high", "fifty_two_week_low", "market_cap", "sector", "industry",
)


class FakeProvider(MarketDataProvider):
    """Scriptable provider: pass data in, or make capabilities fail."""

    name = "fake"

    def __init__(
        self,
        *,
        bars: list[Bar] | None = None,
        quote: Quote | None = None,
        fundamentals: FundamentalsSnapshot | None = None,
        news: list[NewsHeadline] | None = None,
        actions: list[AnalystAction] | None = None,
        earnings: list[EarningsEvent] | None = None,
        dividends: list[Dividend] | None = None,
        calendar: dict[dt.date, list[EarningsEvent]] | None = None,
        fail: set[str] | None = None,
        name: str | None = None,
        missing_fields: tuple[str, ...] = (),
    ) -> None:
        self.bars, self.quote, self.fundamentals = bars, quote, fundamentals
        self.news, self.actions, self.earnings = news, actions, earnings
        self.dividends = dividends
        #: Market-wide earnings calendar by day; None = capability unsupported.
        self.calendar = calendar
        self.fail = fail or set()
        self.missing_fields = missing_fields
        self.calls: list[str] = []
        if name:
            self.name = name

    def _result(self, capability: str, payload, endpoint: str) -> ProviderResult:
        self.calls.append(capability)
        if capability in self.fail:
            return ProviderResult(self.name, endpoint, ok=False, error=f"{capability} down")
        rows = tuple(payload or ())
        return ProviderResult(
            provider=self.name, endpoint=endpoint, ok=bool(rows), data=rows,
            row_count=len(rows), missing_fields=self.missing_fields,
        )

    def get_history(self, symbol, start, end, *, asset_class="stock") -> ProviderResult:
        return self._result("history", self.bars, f"history({symbol})")

    def get_quote(self, symbol, *, asset_class="stock") -> ProviderResult:
        if "quote" in self.fail:
            self.calls.append("quote")
            return ProviderResult(self.name, f"quote({symbol})", ok=False, error="quote down")
        if self.quote is None:
            return ProviderResult(self.name, f"quote({symbol})", ok=False, error="no price")
        self.calls.append("quote")
        return ProviderResult(
            provider=self.name, endpoint=f"quote({symbol})", ok=True,
            data=(self.quote,), row_count=1, as_of=self.quote.as_of,
        )

    def get_fundamentals(self, symbol, *, asset_class="stock") -> ProviderResult:
        if self.fundamentals is None:
            self.calls.append("fundamentals")
            return ProviderResult(self.name, f"fundamentals({symbol})", ok=False, error="no info")
        result = self._result("fundamentals", [self.fundamentals], f"fundamentals({symbol})")
        if not self.missing_fields:
            # Real providers report which fields they could not supply; mirror
            # that so ingestion tests exercise the same code path.
            gaps = tuple(
                field for field in _FUNDAMENTAL_FIELDS
                if getattr(self.fundamentals, field, None) is None
            )
            result = ProviderResult(
                provider=result.provider, endpoint=result.endpoint, ok=result.ok,
                data=result.data, row_count=result.row_count, missing_fields=gaps,
                as_of=result.as_of,
            )
        return result

    def get_news(self, symbol, *, asset_class="stock") -> ProviderResult:
        return self._result("news", self.news, f"news({symbol})")

    def get_analyst_actions(self, symbol, *, asset_class="stock") -> ProviderResult:
        return self._result("analyst_actions", self.actions, f"analyst_actions({symbol})")

    def get_earnings(self, symbol, *, asset_class="stock") -> ProviderResult:
        return self._result("earnings", self.earnings, f"earnings({symbol})")

    def get_dividends(self, symbol, start, end, *, asset_class="stock") -> ProviderResult:
        # Like yfinance: an empty list is a successful "pays nothing in range".
        self.calls.append("dividends")
        if "dividends" in self.fail:
            return ProviderResult(self.name, f"dividends({symbol})", ok=False,
                                  error="dividends down")
        rows = tuple(d for d in (self.dividends or ()) if start <= d.ex_date <= end)
        return ProviderResult(self.name, f"dividends({symbol})", ok=True, data=rows,
                              row_count=len(rows))

    def get_earnings_calendar(self, day: dt.date) -> ProviderResult:
        if self.calendar is None:
            return super().get_earnings_calendar(day)
        self.calls.append(f"calendar:{day.isoformat()}")
        if "earnings_calendar" in self.fail:
            return ProviderResult(self.name, f"earnings_calendar({day})", ok=False,
                                  error="calendar down")
        rows = tuple(self.calendar.get(day, ()))  # empty day = success, like Nasdaq
        return ProviderResult(self.name, f"earnings_calendar({day})", ok=True, data=rows,
                              row_count=len(rows))

    def get_universe(self, kind: str = "sp500") -> ProviderResult:
        return self._result("universe", [], f"universe:{kind}")


def make_bar(
    symbol: str = "AAPL", date: dt.date | None = None, close: str = "100.00", **kwargs
) -> Bar:
    """Convenience bar factory for tests."""
    return Bar(
        symbol=symbol, date=date or dt.date(2026, 1, 5), close=Decimal(close), **kwargs
    )
