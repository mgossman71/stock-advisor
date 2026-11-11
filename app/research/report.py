"""Report assembly: DB rows -> indicators -> scores -> persisted report.

The report is pure function of what the DB holds; it fetches nothing.  Callers
run ingestion first (``refresh_symbol``), then ``build_report``.  This keeps
reports reproducible and makes "data as of" an honest timestamp of stored
rows, not of a live call.

Rules:
* Fields with no stored value appear in ``missing_fields`` and render as
  "unavailable" - never interpolated.
* Bull points are strong components (>= 70), bear points weak ones (<= 30),
  risk flags are explicit risk-score failures.
* ``data_as_of`` is the newest ``fetched_at`` among everything cited.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import (
    AnalystAction, EarningsEvent, Fundamentals, NewsItem, PriceHistory, ResearchReport,
    Ticker,
)
from app.research import indicators
from app.research.scoring import (
    Evidence, SubScore, composite, load_weights, momentum_score, quality_score,
    risk_score, sentiment_score, value_score,
)

D = Decimal
LOOKBACK_DAYS = 800  # ~3 years of bars: enough for 12m returns + 200-day SMA

#: Period label -> trading days, for the momentum block.
_RETURN_DAYS = {"1m": 21, "3m": 63, "6m": 126, "12m": 252}


def closes_with_dates(session: Session, ticker_id: int) -> list[tuple[dt.date, Decimal]]:
    rows = session.execute(
        select(PriceHistory.date, PriceHistory.adj_close, PriceHistory.close)
        .where(PriceHistory.ticker_id == ticker_id)
        .order_by(PriceHistory.date)
    ).all()
    return [(r.date, r.adj_close if r.adj_close is not None else r.close) for r in rows if r.close is not None]


def _latest_fundamentals(session: Session, ticker_id: int) -> Fundamentals | None:
    return session.scalar(
        select(Fundamentals).where(Fundamentals.ticker_id == ticker_id)
        .order_by(Fundamentals.as_of_date.desc()).limit(1)
    )


def _sector_median_pe(session: Session, ticker: Ticker) -> Decimal | None:
    """Median trailing PE across same-sector tickers' latest snapshots."""
    if not ticker.sector:
        return None
    latest = (
        select(Fundamentals.ticker_id, func.max(Fundamentals.as_of_date).label("d"))
        .group_by(Fundamentals.ticker_id).subquery()
    )
    rows = session.execute(
        select(Fundamentals.pe_trailing)
        .join(latest, (Fundamentals.ticker_id == latest.c.ticker_id)
              & (Fundamentals.as_of_date == latest.c.d))
        .join(Ticker, Ticker.id == Fundamentals.ticker_id)
        .where(Ticker.sector == ticker.sector,
               Fundamentals.pe_trailing.isnot(None),
               Fundamentals.pe_trailing > 0)
    ).scalars().all()
    values = sorted(v for v in rows if v is not None)
    if not values:
        return None
    mid = len(values) // 2
    if len(values) % 2:
        return values[mid]
    return (values[mid - 1] + values[mid]) / D("2")


def build_report(session: Session, symbol: str, *, persist: bool = True) -> ResearchReport | None:
    """Score one ticker from stored data and (optionally) persist the report.

    Returns ``None`` when the ticker is unknown.  A ticker with *some* prices
    but no fundamentals still gets a report - with the value/quality blocks
    marked unavailable - which is the spec's "never fabricate" requirement.
    """
    ticker = session.scalar(select(Ticker).where(Ticker.symbol == symbol.upper()))
    if ticker is None:
        return None

    bars = closes_with_dates(session, ticker.id)
    closes = [c for _, c in bars]
    fund = _latest_fundamentals(session, ticker.id)

    # ---- indicators --------------------------------------------------------
    returns = {label: indicators.period_return(closes, days)
               for label, days in _RETURN_DAYS.items()}
    rsi_value = indicators.rsi(closes)
    dist_high = indicators.distance_from_high_pct(closes)
    sma50, sma200 = indicators.sma(closes, 50), indicators.sma(closes, 200)
    above50 = closes[-1] > sma50 if closes and sma50 is not None else None
    above200 = closes[-1] > sma200 if closes and sma200 is not None else None
    vol = indicators.annualized_volatility_pct(closes)
    drawdown = indicators.max_drawdown_pct(closes[-252:]) if len(closes) >= 2 else None

    # ---- analyst / news / earnings aggregates ------------------------------
    cutoff90 = dt.date.today() - dt.timedelta(days=90)
    actions = session.scalars(
        select(AnalystAction).where(AnalystAction.ticker_id == ticker.id,
                                    AnalystAction.action_date >= cutoff90)
    ).all()
    upgrades: int | None = len([a for a in actions if a.action in ("upgrade", "target_raise")]) \
        if actions else None
    downgrades: int | None = len([a for a in actions if a.action in ("downgrade", "target_cut")]) \
        if actions else None
    if not actions and fund is None:
        upgrades = downgrades = None

    target = fund.target_price_consensus if fund else None
    price = closes[-1] if closes else (fund.current_price if fund else None)
    upside = (target / price - D("1")) * D("100") if target and price else None

    news_avg = session.scalar(
        select(func.avg(NewsItem.sentiment_score)).where(
            NewsItem.ticker_id == ticker.id, NewsItem.sentiment_score.isnot(None),
            NewsItem.published_at >= dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30),
        )
    )
    last_reported = session.scalar(
        select(EarningsEvent).where(
            EarningsEvent.ticker_id == ticker.id, EarningsEvent.status == "reported",
            EarningsEvent.surprise_pct.isnot(None),
        ).order_by(EarningsEvent.event_date.desc()).limit(1)
    )
    surprise = last_reported.surprise_pct if last_reported else None

    # ---- scoring -----------------------------------------------------------
    subs = [
        value_score(fund, _sector_median_pe(session, ticker), ticker.sector),
        momentum_score(returns, rsi_value, dist_high, above50, above200),
        quality_score(fund),
        sentiment_score(upgrades, downgrades, upside, news_avg),
        risk_score(vol, drawdown, surprise, fund.short_pct_float if fund else None),
    ]
    weights = load_weights(session)
    score = composite(subs, weights)
    stance = (
        "neutral" if score is None
        else "bullish" if score >= 60
        else "bearish" if score <= 40 else "neutral"
    )

    # ---- narrative + evidence ------------------------------------------------
    def _plain(s: SubScore, c: Component) -> str:
        """One readable bullet: category + the component's own plain-English note."""
        return f"{s.name.title()}: {c.note}"

    strong = [c for s in subs for c in s.components if c.score is not None and c.score >= 70]
    weak = [c for s in subs for c in s.components if c.score is not None and c.score <= 30]
    bull_points = [_plain(s, c) for s in subs for c in s.components
                   if c.score is not None and c.score >= 70]
    bear_points = [_plain(s, c) for s in subs for c in s.components
                   if c.score is not None and c.score <= 30]
    risks: list[str] = []
    if vol is not None and vol >= D("35"):
        risks.append(f"high volatility: {vol}% annualised")
    if drawdown is not None and drawdown <= D("-25"):
        risks.append(f"deep drawdown: {drawdown}% over 252 sessions")
    if surprise is not None and surprise <= 0:
        risks.append(f"last earnings surprise was {surprise}%")
    if fund and fund.short_pct_float is not None and fund.short_pct_float >= D("10"):
        risks.append(f"heavily shorted: {fund.short_pct_float}% of float")
    if rsi_value is not None and rsi_value >= D("80"):
        risks.append(f"overbought: RSI {rsi_value}")

    missing = [c.name for s in subs for c in s.components if c.score is None]

    evidence: list[dict] = []
    for s in subs:
        for c in s.components:
            if c.evidence is not None:
                evidence.append(c.evidence.as_dict())
    if closes:
        evidence.append(Evidence("last close", str(closes[-1]), "price_history",
                                 bars[-1][0].isoformat()).as_dict())

    as_of_candidates: list[dt.datetime] = []
    if bars:
        as_of_candidates.append(
            dt.datetime.combine(bars[-1][0], dt.time(), tzinfo=dt.timezone.utc)
        )
    if fund:
        as_of_candidates.append(
            dt.datetime.combine(fund.as_of_date, dt.time(), tzinfo=dt.timezone.utc)
        )
    data_as_of = max(as_of_candidates) if as_of_candidates else None

    score_text = f"{score}/100" if score is not None else "unrateable (no usable data)"
    summary = (
        f"{ticker.symbol} ({ticker.name or 'name unavailable'}): {stance}, composite "
        f"{score_text}. "
        + (f"Bull: {len(bull_points)} factor(s); bear: {len(bear_points)}; "
           f"risk flags: {len(risks)}. "
           f"{len(missing)} input(s) unavailable." if missing else "All inputs available.")
    )

    payload = {
        "weights_used": {k: str(v) for k, v in weights.items()},
        "sub_scores": [s.as_dict() for s in subs],
        "bull_points": bull_points,
        "bear_points": bear_points,
        "risks": risks,
        "evidence": evidence,
        "missing_fields": missing,
        "indicators": {
            "returns_pct": {k: str(v) if v is not None else None for k, v in returns.items()},
            "rsi14": str(rsi_value) if rsi_value is not None else None,
            "vol_annualised_pct": str(vol) if vol is not None else None,
            "max_drawdown_pct": str(drawdown) if drawdown is not None else None,
            "sma50": str(sma50) if sma50 is not None else None,
            "sma200": str(sma200) if sma200 is not None else None,
            "distance_from_52w_high_pct": str(dist_high) if dist_high is not None else None,
            "analyst_upgrades_90d": upgrades,
            "analyst_downgrades_90d": downgrades,
            "target_upside_pct": str(upside) if upside is not None else None,
        },
        "strong_components": [c.name for c in strong],
        "weak_components": [c.name for c in weak],
    }

    report = ResearchReport(
        ticker_id=ticker.id,
        model_or_rule_version="rules-v1",
        data_as_of=data_as_of,
        summary=summary,
        overall_stance=stance,
        score=score if score is not None else D("0"),
        payload_json=payload,
    )
    if persist:
        session.add(report)
        session.flush()
    return report
