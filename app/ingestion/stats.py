"""Read-only reporting (row counts, coverage, fetch log) shared by CLI and web.

Kept separate from the CLI so the web layer reports exactly what `app.cli stats`
reports instead of re-deriving it with different SQL.
"""
from __future__ import annotations

from sqlalchemy import func, inspect, select, text
from sqlalchemy.orm import Session

from app.db.models import (
    AnalystAction,
    DataFetchLog,
    EarningsEvent,
    Fundamentals,
    NewsItem,
    PriceHistory,
    Ticker,
)

#: Tables counted for the status report.  Trading/advice tables are appended as
#: those phases land; tables the current revision has not created are omitted.
REPORT_TABLES: tuple[tuple[str, type], ...] = (
    ("tickers", Ticker),
    ("price_history", PriceHistory),
    ("fundamentals", Fundamentals),
    ("news_items", NewsItem),
    ("analyst_actions", AnalystAction),
    ("earnings_events", EarningsEvent),
    ("data_fetch_log", DataFetchLog),
)

#: Per-symbol row counts shown next to price coverage.
_DETAIL_TABLES: tuple[tuple[str, type], ...] = (
    ("fundamentals", Fundamentals),
    ("news", NewsItem),
    ("analyst_actions", AnalystAction),
    ("earnings", EarningsEvent),
)


def table_counts(session: Session) -> dict[str, int]:
    """Row count per reported table; missing tables are omitted, not reported as 0."""
    existing = _existing_tables(session)
    return {
        name: int(session.scalar(select(func.count()).select_from(model)) or 0)
        for name, model in REPORT_TABLES
        if model.__tablename__ in existing
    }


def coverage_by_ticker(session: Session) -> list[dict[str, object]]:
    """Bars and date span per symbol, plus how much side data exists for it.

    Bars without fundamentals is a symbol we can chart but not score, so both
    counts are reported instead of the bar count alone.
    """
    existing = _existing_tables(session)
    if PriceHistory.__tablename__ not in existing:
        return []

    rows = session.execute(
        select(
            Ticker.symbol,
            func.count(PriceHistory.id),
            func.min(PriceHistory.date),
            func.max(PriceHistory.date),
        )
        .join(PriceHistory, PriceHistory.ticker_id == Ticker.id)
        .group_by(Ticker.symbol)
        .order_by(func.count(PriceHistory.id).desc())
    ).all()

    details: dict[str, dict[str, int]] = {}
    for label, model in _DETAIL_TABLES:
        if model.__tablename__ not in existing:
            continue
        for symbol, count in session.execute(
            select(Ticker.symbol, func.count(model.id))
            .join(model, model.ticker_id == Ticker.id)
            .group_by(Ticker.symbol)
        ).all():
            details.setdefault(symbol, {})[label] = int(count)

    return [
        {
            "symbol": symbol,
            "bars": int(count),
            "first_date": str(low),
            "last_date": str(high),
            **details.get(symbol, {}),
        }
        for symbol, count, low, high in rows
    ]


def recent_fetch_log(session: Session, limit: int = 10) -> list[dict[str, object]]:
    """Most recent provider calls, newest first, failures included."""
    if DataFetchLog.__tablename__ not in _existing_tables(session):
        return []
    return [
        {
            "fetched_at": entry.fetched_at,
            "provider": entry.provider,
            "endpoint": entry.endpoint,
            "status": entry.status,
            "rows": int(entry.row_count or 0),
            "error": entry.error,
        }
        for entry in session.scalars(
            select(DataFetchLog).order_by(DataFetchLog.id.desc()).limit(int(limit))
        )
    ]


#: Catalog query per dialect, run on the session's own connection.
_CATALOG_QUERIES: dict[str, str] = {
    "sqlite": "SELECT name FROM sqlite_master WHERE type='table'",
    "postgresql": (
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = ANY(current_schemas(false))"
    ),
}


def table_names(session: Session) -> set[str]:
    """Tables physically present, so a half-migrated database still reports.

    The catalog is read on the session's own connection rather than through
    ``sqlalchemy.inspect``: inspecting an engine takes out a separate connection,
    which for in-memory SQLite (a database that *is* its connection) discards the
    data being reported on.
    """
    dialect = session.get_bind().dialect.name
    query = _CATALOG_QUERIES.get(dialect)
    if query is None:  # pragma: no cover - only reachable on an unlisted backend
        return set(inspect(session.get_bind()).get_table_names())
    return set(session.execute(text(query)).scalars())


def _existing_tables(session: Session) -> set[str]:
    return table_names(session)
