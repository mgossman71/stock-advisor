"""Nightly screening: full reports + recommendations for the scan shortlist.

Fetch policy lives in the *job* (``app.jobs``), not here: the screener only
scores what the DB already holds, so it is fast, offline-safe and idempotent.
Tickers with too little price history to score are counted and reported,
never scored on noise.
"""

from __future__ import annotations

import datetime as dt
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import (
    AnalystAction, EarningsEvent, PriceHistory, Recommendation, ResearchReport, Ticker,
)
from app.background import Progress
from app.research import emerging
from app.research.engine import AdvisorEngine, RuleBasedAdvisor
from app.research.llm_advisor import LLMAdvisor, advisor_for
from app.trading.orders import latest_price

D = Decimal
MIN_BARS_TO_SCORE = 60  # under ~3 months of bars the momentum block is noise


@dataclass
class ScreenStats:
    """Counters for one screening pass."""

    scored: int = 0
    #: Universe stocks scan-scored before this screen (``run_screen_only``).
    scanned: int = 0
    skipped_short_history: int = 0
    skipped_unknown: int = 0
    #: AI analyst use: fresh calls, reports reused (nothing material changed),
    #: and names that got the rules report because the budget ran out.
    ai_used: int = 0
    ai_reused: int = 0
    ai_over_budget: int = 0
    new_recommendations: list[Recommendation] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def screening_candidates(session: Session) -> list[Ticker]:
    """The funnel's deep stage, in priority order: everything the user holds
    or watches (watchlist, open paper positions, real-ledger lots), then the
    reviewed emerging-lens candidates by confirmed score, then the composite
    shortlist.  The order is the order the AI budget is spent in.  The rest
    of the universe is only scanned."""
    from app.db.models import Position
    from app.research.scan import emerging_priority, shortlist_symbols
    from app.tracking.lots import open_lots

    owned = set(session.scalars(select(Ticker.symbol).where(Ticker.in_watchlist.is_(True))))
    owned.update(session.scalars(
        select(Ticker.symbol).join(Position, Position.ticker_id == Ticker.id)
        .where(Position.qty != 0)))
    owned.update(lot.trade.ticker.symbol for lot in open_lots(session))
    # Priority order = AI-time order: what you own or watch, then early movers
    # by confirmed (lens + catalyst) score, then the composite shortlist.
    ordered: list[str] = []
    for symbol in [*sorted(owned), *emerging_priority(session), *shortlist_symbols(session)]:
        if symbol not in ordered:
            ordered.append(symbol)
    if not ordered:
        return []
    by_symbol = {t.symbol: t for t in session.scalars(
        select(Ticker).where(Ticker.symbol.in_(ordered), Ticker.asset_class != "index"))}
    return [by_symbol[s] for s in ordered if s in by_symbol]


#: An AI report is reused (no new model call) while it is this fresh and
#: nothing material moved: price, a new report/rating, or the lens score.
REUSE_DAYS = 7
REUSE_PRICE_PCT = D("5")
REUSE_LENS_POINTS = D("10")


class AIBudget:
    """Wall-clock allowance for model calls in one screen (``clock`` is
    injectable so tests never sleep)."""

    def __init__(self, minutes: int, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._deadline = clock() + minutes * 60

    def left(self) -> bool:
        return self._clock() < self._deadline


def reusable_ai_report(session: Session, ticker: Ticker, today: dt.date,
                       lens_score: Decimal | None = None) -> ResearchReport | None:
    """The ticker's recent AI report, if nothing material changed since it."""
    report = session.scalars(
        select(ResearchReport).where(ResearchReport.ticker_id == ticker.id,
                                     ResearchReport.model_or_rule_version.like("llm:%"))
        .order_by(ResearchReport.created_at.desc()).limit(1)).first()
    if report is None or report.created_at is None:
        return None
    made = report.created_at.date()
    if (today - made).days > REUSE_DAYS:
        return None
    then = session.scalar(select(PriceHistory.close).where(
        PriceHistory.ticker_id == ticker.id, PriceHistory.date <= made,
        PriceHistory.close.isnot(None)).order_by(PriceHistory.date.desc()).limit(1))
    now = latest_price(session, ticker.id)
    if then is None or now is None or abs(now[0] / then - 1) * 100 > REUSE_PRICE_PCT:
        return None
    if session.scalar(select(func.count(EarningsEvent.id)).where(
            EarningsEvent.ticker_id == ticker.id, EarningsEvent.status == "reported",
            EarningsEvent.event_date > made)):
        return None
    if session.scalar(select(func.count(AnalystAction.id)).where(
            AnalystAction.ticker_id == ticker.id, AnalystAction.action_date > made)):
        return None
    stored = (report.payload_json or {}).get("lens_score")
    if stored is not None and lens_score is not None \
            and abs(D(stored) - lens_score) > REUSE_LENS_POINTS:
        return None
    return report


def screen_universe(
    session: Session, *, advisor: AdvisorEngine | None = None,
    limit: int | None = None, progress: Progress | None = None,
    commit_each: bool = False, clock: Callable[[], float] | None = None,
) -> ScreenStats:
    """Build a report + recommendations for every candidate; returns counters.

    ``commit_each`` commits after every ticker - background runs use it so a
    slow model never holds SQLite's write lock across the whole universe.

    With the AI analyst on, candidates are taken in priority order and the
    model is used until ``ai_minutes_per_night`` runs out; a recent AI report
    is reused when nothing material changed (``reusable_ai_report``), and
    names past the budget get the rules report (marked ``ai_skipped``).
    """
    from app.research.scan import emerging_symbols, latest_emerging

    advisor = advisor or advisor_for(session, screening=True)
    llm = advisor if isinstance(advisor, LLMAdvisor) else None
    rules = RuleBasedAdvisor()
    budget = AIBudget(llm.config.ai_minutes_per_night, clock or time.monotonic) if llm else None
    today = dt.date.today()
    stats = ScreenStats()
    candidates = screening_candidates(session)
    emerging_set = set(emerging_symbols(session))
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
        lens_row = latest_emerging(session, ticker.id)
        lens_score = lens_row.emerging_score if lens_row is not None else None
        engine: AdvisorEngine = advisor
        reused = None
        if llm is not None:
            reused = reusable_ai_report(session, ticker, today, lens_score)
            if reused is not None:
                stats.ai_reused += 1
            elif budget.left():
                stats.ai_used += 1
            else:
                engine = rules
                stats.ai_over_budget += 1
        report = reused or engine.research(session, ticker.symbol)
        if report is None:
            stats.skipped_unknown += 1
            continue
        if reused is None:
            extra = {"lens_score": str(lens_score)} if lens_score is not None else {}
            if llm is not None and engine is rules:
                extra["ai_skipped"] = "budget"
            if extra:  # reassign so the JSON change is persisted
                report.payload_json = {**(report.payload_json or {}), **extra}
                session.flush()
        stats.scored += 1
        stats.new_recommendations.extend(
            (llm or engine).recommendations_for(session, report))
        if ticker.symbol in emerging_set:
            call = emerging_recommendation(session, report)
            if call is not None:
                stats.new_recommendations.append(call)
        if commit_each:
            session.commit()
    session.flush()
    if llm is not None:
        stats.notes.append(f"AI analyst: {stats.ai_used} read, {stats.ai_reused} reused "
                           f"(unchanged), {stats.ai_over_budget} past the "
                           f"{llm.config.ai_minutes_per_night}-min budget (rules report)")
    return stats


def emerging_recommendation(session: Session, report: ResearchReport) -> Recommendation | None:
    """An ``emerging-v1`` long for a lens candidate that survived full research.

    Fires when the lens score is >= ``emerging.CALL_MIN_SCORE`` and the full
    report doesn't object: with the AI analyst on, its stance must be
    *bullish* (it has read the news and judged the catalyst); on the rules
    engine alone, merely not bearish (rules penalise the expensive, volatile
    look that breakouts have).  Entry is the stored close; stop -8 %, target
    +25 %, short horizon.  The usual one-open-call-per-direction rule applies.
    """
    from app.research.scan import latest_emerging

    row = latest_emerging(session, report.ticker_id)
    if row is None or row.emerging_score < emerging.CALL_MIN_SCORE:
        return None
    llm = (report.payload_json or {}).get("llm")
    if (llm and report.overall_stance != "bullish") or report.overall_stance == "bearish":
        return None
    priced = latest_price(session, report.ticker_id)
    if priced is None:
        return None  # never invent an entry
    entry = priced[0]
    stop, target = emerging.call_levels(entry)
    reasons = (row.emerging or {}).get("reasons", [])
    rationale = "Emerging: " + "; ".join(reasons[:6])
    confidence = emerging.call_confidence(row.emerging_score)
    if emerging.is_micro(report.ticker.market_cap):
        rationale = "MICRO CAP — high risk. " + rationale
        confidence = min(confidence, emerging.MICRO_CAP_MAX_CONFIDENCE)
    version = emerging.ENGINE_VERSION
    if llm:
        rationale += f" | AI ({llm.get('model')}): {report.summary}"
        version = f"{version}+{report.model_or_rule_version}"
    return RuleBasedAdvisor()._make(
        session, report, "long", entry,
        confidence=confidence,
        stop=stop, target=target, rationale=rationale, horizon="short",
        engine_version=version[:60],
    )
