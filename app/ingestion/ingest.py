"""Fetch -> normalise -> persist, with idempotent upserts and fetch logging.

Idempotency rules (the nightly job calls this constantly, and
``POST /jobs/refresh`` must be safe to hit twice):

* Bars already present for a (ticker, date) are updated in place, never
  duplicated - the unique constraint guarantees it.
* A range already covered up to ``freshness_days`` is skipped and logged as
  ``skipped_fresh``; that cache is what keeps yfinance quiet.
* Every provider call writes exactly one ``data_fetch_log`` row with status
  ``ok`` / ``empty`` / ``error`` / ``skipped_fresh``.
"""

from __future__ import annotations

import datetime as dt
import time
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    AnalystAction, DataFetchLog, EarningsEvent, Fundamentals, NewsItem,
    PriceHistory, Ticker,
)
from app.providers.base import (
    Bar, EarningsEvent as EarningsPayload, FundamentalsSnapshot, MarketDataProvider,
    NewsHeadline, ProviderResult, Quote, Side,
)


@dataclass
class IngestStats:
    """Outcome of one ingestion pass, surfaced by the CLI and job endpoints."""

    symbol: str
    fetched: int = 0
    written: int = 0
    skipped: int = 0
    notes: list[str] = field(default_factory=list)

    def note(self, message: str) -> None:
        """Record a human-readable remark (provider failures, skips)."""
        self.notes.append(message)


def ensure_ticker(
    session: Session, symbol: str, *, name: str | None = None, sector: str | None = None,
    industry: str | None = None, currency: str | None = None,
    market_cap: Decimal | None = None, asset_class: str = "stock",
    in_universe: bool | None = None, in_watchlist: bool | None = None,
) -> Ticker:
    """Return the ``Ticker`` for ``symbol``, creating it when missing."""
    symbol = symbol.upper().strip()
    ticker = session.scalar(select(Ticker).where(Ticker.symbol == symbol))
    if ticker is None:
        ticker = Ticker(symbol=symbol, asset_class=asset_class)
        session.add(ticker)
        session.flush()
    # Only fill in what we actually have - never overwrite a good value with None.
    for attribute, value in (
        ("name", name), ("sector", sector), ("industry", industry),
        ("currency", currency), ("market_cap", market_cap), ("asset_class", asset_class),
    ):
        if value is not None:
            setattr(ticker, attribute, value)
    if in_universe is not None:
        ticker.in_universe = in_universe
    if in_watchlist is not None:
        ticker.in_watchlist = in_watchlist
    session.flush()
    return ticker


def log_fetch(
    session: Session, *, provider: str, endpoint: str, ticker: str | None, status: str,
    error: str | None = None, row_count: int | None = None, latency_ms: int | None = None,
) -> DataFetchLog:
    """Append one ``data_fetch_log`` row (audit trail + freshness evidence)."""
    entry = DataFetchLog(
        provider=provider, endpoint=endpoint, ticker=ticker, status=status,
        error=error, row_count=row_count, latency_ms=latency_ms,
    )
    session.add(entry)
    return entry


def covered_range(
    session: Session, ticker_id: int, start: dt.date, end: dt.date
) -> tuple[dt.date | None, dt.date | None, int]:
    """Earliest/latest stored bar for ``ticker_id`` inside ``[start, end]``."""
    rows = session.execute(
        select(PriceHistory.date).where(
            PriceHistory.ticker_id == ticker_id,
            PriceHistory.date >= start,
            PriceHistory.date <= end,
        )
    ).scalars().all()
    if not rows:
        return None, None, 0
    return min(rows), max(rows), len(rows)


def ingest_history(
    session: Session, provider: MarketDataProvider, symbol: str, start: dt.date, end: dt.date,
    *, asset_class: Side = "stock", freshness_days: int = 1,
) -> IngestStats:
    """Fetch and upsert daily bars, skipping ranges that are already fresh."""
    stats = IngestStats(symbol=symbol.upper())
    ticker = ensure_ticker(session, symbol)

    low, high, count = covered_range(session, ticker.id, start, end)
    if high is not None and high >= end - dt.timedelta(days=freshness_days):
        log_fetch(
            session, provider=provider.name, endpoint=f"history({symbol})",
            ticker=symbol, status="skipped_fresh", row_count=count,
        )
        stats.skipped = count
        stats.note(f"history covers {low}..{high} ({count} bars) - skipped")
        return stats

    started = time.monotonic()
    result = provider.get_history(symbol, start, end, asset_class=asset_class)
    latency = int((time.monotonic() - started) * 1000)
    if not result.ok:
        log_fetch(
            session, provider=result.provider, endpoint=result.endpoint, ticker=symbol,
            status="error", error=result.error, row_count=0, latency_ms=latency,
        )
        stats.note(f"history unavailable: {result.error}")
        return stats

    existing = {
        row.date: row
        for row in session.scalars(
            select(PriceHistory).where(
                PriceHistory.ticker_id == ticker.id,
                PriceHistory.date >= start,
                PriceHistory.date <= end,
            )
        )
    }
    for bar in result.data:
        # Provenance is taken from the result envelope, not the bar: a provider
        # that forgets to stamp ``Bar.source`` cannot mislabel stored rows.
        _upsert_bar(session, ticker.id, bar, existing, source=result.provider)
        stats.written += 1
    stats.fetched = result.row_count
    log_fetch(
        session, provider=result.provider, endpoint=result.endpoint, ticker=symbol,
        status="ok", row_count=stats.written, latency_ms=latency,
    )
    return stats


def _upsert_bar(
    session: Session, ticker_id: int, bar: Bar, existing: dict[dt.date, PriceHistory],
    *, source: str | None = None,
) -> None:
    """Insert or update one bar; unique (ticker, date) makes this safe."""
    provenance = source or bar.source
    row = existing.get(bar.date)
    if row is None:
        session.add(PriceHistory(
            ticker_id=ticker_id, date=bar.date, open=bar.open, high=bar.high,
            low=bar.low, close=bar.close, adj_close=bar.adj_close,
            volume=bar.volume, source=provenance,
        ))
        return
    row.open, row.high, row.low = bar.open, bar.high, bar.low
    row.close, row.adj_close, row.volume = bar.close, bar.adj_close, bar.volume
    row.source = provenance
    row.fetched_at = dt.datetime.now(dt.timezone.utc)


def ingest_fundamentals(
    session: Session, provider: MarketDataProvider, symbol: str,
    *, asset_class: Side = "stock",
) -> IngestStats:
    """Persist one fundamentals snapshot and enrich the ticker row."""
    stats = IngestStats(symbol=symbol.upper())
    result = provider.get_fundamentals(symbol, asset_class=asset_class)
    if not result.ok:
        log_fetch(session, provider=provider.name, endpoint=f"fundamentals({symbol})",
                  ticker=symbol, status="error", error=result.error, row_count=0)
        stats.note(f"fundamentals unavailable: {result.error}")
        return stats

    snapshot: FundamentalsSnapshot = result.data[0]
    ticker = ensure_ticker(
        session, symbol, name=snapshot.name, sector=snapshot.sector,
        industry=snapshot.industry, currency=snapshot.currency, market_cap=snapshot.market_cap,
    )
    existing = session.scalar(
        select(Fundamentals).where(
            Fundamentals.ticker_id == ticker.id,
            Fundamentals.as_of_date == snapshot.as_of_date,
        )
    )
    row = existing or Fundamentals(ticker_id=ticker.id, as_of_date=snapshot.as_of_date)
    for attribute in (
        "pe_trailing", "pe_forward", "ev_ebitda", "ps", "pb", "debt_to_equity",
        "revenue_growth_yoy", "eps_growth_yoy", "dividend_yield", "fcf_yield",
        "gross_margin", "operating_margin", "profit_margin", "return_on_equity",
        "current_price", "target_price_consensus", "short_pct_float", "beta",
        "fifty_two_week_high", "fifty_two_week_low", "market_cap",
    ):
        value = getattr(snapshot, attribute, None)
        if value is not None:
            setattr(row, attribute, value)
    row.source = snapshot.source
    row.fetched_at = dt.datetime.now(dt.timezone.utc)
    if existing is None:
        session.add(row)
    session.flush()

    stats.written = 1
    log_fetch(session, provider=result.provider, endpoint=result.endpoint, ticker=symbol,
              status="ok", row_count=1)
    if snapshot.pe_history:
        stats.note(f"PE history: {len(snapshot.pe_history)} periods")
    if result.missing_fields:
        # Full list, not a truncated one: "what we could not get" is itself a
        # finding the report has to disclose.
        stats.note(
            f"{len(result.missing_fields)} fields unavailable: "
            f"{', '.join(result.missing_fields)}"
        )
    return stats


def ingest_quote(
    session: Session, provider: MarketDataProvider, symbol: str,
    *, asset_class: Side = "stock",
) -> tuple[IngestStats, Quote | None]:
    """Fetch the latest price and store it as today's bar (upsert)."""
    stats = IngestStats(symbol=symbol.upper())
    result = provider.get_quote(symbol, asset_class=asset_class)
    if not result.ok:
        log_fetch(session, provider=provider.name, endpoint=f"quote({symbol})",
                  ticker=symbol, status="error", error=result.error, row_count=0)
        stats.note(f"quote unavailable: {result.error}")
        return stats, None

    quote: Quote = result.data[0]
    ticker = ensure_ticker(session, symbol, name=quote.name, currency=quote.currency,
                           market_cap=quote.market_cap)
    today = quote.as_of.date()
    existing = {
        row.date: row
        for row in session.scalars(
            select(PriceHistory).where(
                PriceHistory.ticker_id == ticker.id, PriceHistory.date == today
            )
        )
    }
    # A quote carries no OHLC, so those columns stay NULL rather than being
    # back-filled with the close - the row's source says which provider priced it.
    bar = Bar(symbol=symbol.upper(), date=today, close=quote.price, source=result.provider)
    _upsert_bar(session, ticker.id, bar, existing, source=result.provider)
    session.flush()
    stats.written = 1
    log_fetch(session, provider=result.provider, endpoint=result.endpoint, ticker=symbol,
              status="ok", row_count=1)
    return stats, quote


def ingest_news(
    session: Session, provider: MarketDataProvider, symbol: str,
    *, asset_class: Side = "stock", limit: int = 25,
) -> IngestStats:
    """Persist new headlines (deduped on ticker+url)."""
    stats = IngestStats(symbol=symbol.upper())
    result = provider.get_news(symbol, asset_class=asset_class)
    if not result.ok:
        log_fetch(session, provider=provider.name, endpoint=f"news({symbol})",
                  ticker=symbol, status="error", error=result.error, row_count=0)
        stats.note(f"news unavailable: {result.error}")
        return stats

    ticker = ensure_ticker(session, symbol)
    seen = set(session.scalars(
        select(NewsItem.url).where(NewsItem.ticker_id == ticker.id)
    ))
    for headline in result.data[:limit]:
        # url is part of the unique key and of every citation the report makes,
        # so a headline without one is useless - drop it rather than store it.
        if not headline.url or not headline.title:
            stats.skipped += 1
            continue
        if headline.url in seen:
            stats.skipped += 1
            continue
        seen.add(headline.url)
        session.add(NewsItem(
            ticker_id=ticker.id, published_at=headline.published_at, source=headline.source,
            title=headline.title, url=headline.url, sentiment_score=headline.sentiment_score,
            body_excerpt=headline.body_excerpt, provider=headline.provider,
        ))
        stats.written += 1
    session.flush()
    log_fetch(session, provider=result.provider, endpoint=result.endpoint, ticker=symbol,
              status="ok" if stats.written else "empty", row_count=stats.written)
    return stats


def ingest_analyst_actions(
    session: Session, provider: MarketDataProvider, symbol: str,
    *, asset_class: Side = "stock", lookback_days: int = 365,
) -> IngestStats:
    """Persist rating / target actions inside the lookback window."""
    stats = IngestStats(symbol=symbol.upper())
    result = provider.get_analyst_actions(symbol, asset_class=asset_class)
    if not result.ok:
        log_fetch(session, provider=provider.name, endpoint=f"analyst_actions({symbol})",
                  ticker=symbol, status="error", error=result.error, row_count=0)
        stats.note(f"analyst actions unavailable: {result.error}")
        return stats

    ticker = ensure_ticker(session, symbol)
    cutoff = dt.date.today() - dt.timedelta(days=lookback_days)
    existing = {
        (row.action_date, row.firm, row.action)
        for row in session.scalars(
            select(AnalystAction).where(
                AnalystAction.ticker_id == ticker.id, AnalystAction.action_date >= cutoff
            )
        )
    }
    for action in result.data:
        if action.action_date < cutoff:
            stats.skipped += 1
            continue
        key = (action.action_date, action.firm, action.action)
        if key in existing:
            stats.skipped += 1
            continue
        existing.add(key)
        session.add(AnalystAction(
            ticker_id=ticker.id, action_date=action.action_date, firm=action.firm,
            action=action.action, old_rating=action.old_rating, new_rating=action.new_rating,
            old_target=action.old_target, new_target=action.new_target, source=action.source,
        ))
        stats.written += 1
    session.flush()
    log_fetch(session, provider=result.provider, endpoint=result.endpoint, ticker=symbol,
              status="ok" if stats.written else "empty", row_count=stats.written)
    return stats


def ingest_earnings(
    session: Session, provider: MarketDataProvider, symbol: str,
    *, asset_class: Side = "stock",
) -> IngestStats:
    """Persist earnings dates, keeping estimates for upcoming quarters."""
    stats = IngestStats(symbol=symbol.upper())
    result = provider.get_earnings(symbol, asset_class=asset_class)
    if not result.ok:
        log_fetch(session, provider=provider.name, endpoint=f"earnings({symbol})",
                  ticker=symbol, status="error", error=result.error, row_count=0)
        stats.note(f"earnings unavailable: {result.error}")
        return stats

    ticker = ensure_ticker(session, symbol)
    for event in result.data:
        row = session.scalar(
            select(EarningsEvent).where(
                EarningsEvent.ticker_id == ticker.id,
                EarningsEvent.event_date == event.event_date,
            )
        ) or EarningsEvent(ticker_id=ticker.id, event_date=event.event_date)
        for attribute in (
            "eps_estimate", "eps_actual", "revenue_estimate", "revenue_actual", "surprise_pct"
        ):
            value = getattr(event, attribute, None)
            if value is not None:
                setattr(row, attribute, value)
        row.status = event.status
        row.source = event.source
        row.fetched_at = dt.datetime.now(dt.timezone.utc)
        session.add(row)
        stats.written += 1
    session.flush()
    log_fetch(session, provider=result.provider, endpoint=result.endpoint, ticker=symbol,
              status="ok", row_count=stats.written)
    return stats


def refresh_symbol(
    session: Session, provider: MarketDataProvider, symbol: str, *,
    start: dt.date | None = None, end: dt.date | None = None, asset_class: Side = "stock",
    with_alternatives: bool = True,
) -> dict[str, IngestStats]:
    """Run the whole fetch/normalise/persist pipeline for one symbol."""
    end = end or dt.date.today()
    start = start or end - dt.timedelta(days=365 * 2)
    outcomes = {
        "history": ingest_history(session, provider, symbol, start, end, asset_class=asset_class),
        "fundamentals": ingest_fundamentals(session, provider, symbol, asset_class=asset_class),
    }
    # A live quote guarantees today's price lands even when the provider's
    # daily bar for the current session is still half-formed (Yahoo publishes
    # a row with a NaN close intraday, which history ingestion drops).
    outcomes["quote"] = ingest_quote(session, provider, symbol, asset_class=asset_class)[0]
    if with_alternatives:
        outcomes["news"] = ingest_news(session, provider, symbol, asset_class=asset_class)
        outcomes["analyst_actions"] = ingest_analyst_actions(
            session, provider, symbol, asset_class=asset_class
        )
        outcomes["earnings"] = ingest_earnings(session, provider, symbol, asset_class=asset_class)
    session.flush()
    return outcomes



