"""Nightly screening: full reports + recommendations for the scan shortlist.

Fetch policy lives in the *job* (``app.jobs``), not here: the screener only
scores what the DB already holds, so it is fast, offline-safe and idempotent.
Tickers with too little price history to score are counted and reported,
never scored on noise.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import PriceHistory, Recommendation, Ticker
from app.background import Progress
from app.research.engine import AdvisorEngine
from app.research.llm_advisor import advisor_for

MIN_BARS_TO_SCORE = 60  # under ~3 months of bars the momentum block is noise


@dataclass
class ScreenStats:
    """Counters for one screening pass."""

    scored: int = 0
    #: Universe stocks scan-scored before this screen (``run_screen_only``).
    scanned: int = 0
    skipped_short_history: int = 0
    skipped_unknown: int = 0
    new_recommendations: list[Recommendation] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def screening_candidates(session: Session) -> list[Ticker]:
    """The funnel's deep stage: the latest scan's shortlist, plus everything
    the user holds or watches (watchlist, open paper positions, real-ledger
    lots).  The rest of the universe is only scanned - that is what keeps a
    ~3,000-stock universe affordable when the LLM screens."""
    from app.db.models import Position
    from app.research.scan import shortlist_symbols
    from app.tracking.lots import open_lots

    symbols = set(shortlist_symbols(session))
    symbols.update(session.scalars(select(Ticker.symbol).where(Ticker.in_watchlist.is_(True))))
    symbols.update(session.scalars(
        select(Ticker.symbol).join(Position, Position.ticker_id == Ticker.id)
        .where(Position.qty != 0)))
    symbols.update(lot.trade.ticker.symbol for lot in open_lots(session))
    if not symbols:
        return []
    return list(session.scalars(
        select(Ticker).where(Ticker.symbol.in_(symbols), Ticker.asset_class != "index")
        .order_by(Ticker.symbol)))


def screen_universe(
    session: Session, *, advisor: AdvisorEngine | None = None,
    limit: int | None = None, progress: Progress | None = None,
    commit_each: bool = False,
) -> ScreenStats:
    """Build a report + recommendations for every candidate; returns counters.

    ``commit_each`` commits after every ticker - background runs use it so a
    slow model never holds SQLite's write lock across the whole universe.
    """
    advisor = advisor or advisor_for(session, screening=True)
    stats = ScreenStats()
    candidates = screening_candidates(session)
    if limit:
        candidates = candidates[:limit]
    for index, ticker in enumerate(candidates, start=1):
        if progress is not None:
            progress("screening", index - 1, len(candidates), ticker.symbol)
        count = session.scalar(
            select(func.count(PriceHistory.id)).where(PriceHistory.ticker_id == ticker.id)
        ) or 0
        if count < MIN_BARS_TO_SCORE:
            stats.skipped_short_history += 1
            stats.notes.append(f"{ticker.symbol}: only {count} bars, not scored")
            continue
        report = advisor.research(session, ticker.symbol)
        if report is None:
            stats.skipped_unknown += 1
            continue
        stats.scored += 1
        stats.new_recommendations.extend(advisor.recommendations_for(session, report))
        if commit_each:
            session.commit()
    session.flush()
    return stats
