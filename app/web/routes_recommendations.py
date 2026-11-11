"""Recommendations: full history, outcomes, alpha scoreboard, manual close."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.db.base import session_scope
from app.db.models import Position, Recommendation
from app.research.outcomes import scoreboard
from app.tracking.lots import open_lots
from app.trading.orders import latest_price
from app.web.common import DISCLAIMER, templates

router = APIRouter()


def _shares(qty: Decimal) -> str:
    """10, 2.5 - never 1E+1 or 10.000000."""
    return f"{qty.normalize():f}"


def holdings_by_ticker(session) -> dict[int, list[str]]:
    """ticker_id -> what is held now, e.g. ["10 paper", "25 real"]."""
    held: dict[int, list[str]] = {}
    paper: dict[int, Decimal] = {}
    for position in session.query(Position).filter(Position.qty != 0):
        paper[position.ticker_id] = paper.get(position.ticker_id, Decimal(0)) + position.qty
    for ticker_id, qty in paper.items():
        if qty:
            label = "paper short" if qty < 0 else "paper"
            held.setdefault(ticker_id, []).append(f"{_shares(abs(qty))} {label}")
    real: dict[int, Decimal] = {}
    for lot in open_lots(session):
        real[lot.trade.ticker_id] = real.get(lot.trade.ticker_id, Decimal(0)) + lot.remaining
    for ticker_id, qty in real.items():
        if qty:
            held.setdefault(ticker_id, []).append(f"{_shares(qty)} real")
    return held


@router.get("/recommendations")
def recommendations_page(request: Request, status: str | None = None,
                         direction: str | None = None, sort: str = "best",
                         held: str = ""):
    settings = get_settings()
    with session_scope() as session:
        holdings = holdings_by_ticker(session)
        query = session.query(Recommendation)
        if sort == "recent":
            query = query.order_by(Recommendation.generated_at.desc())
        else:  # default: best idea first, newest breaks ties
            query = query.order_by(Recommendation.confidence.desc(),
                                   Recommendation.generated_at.desc())
        if status:
            query = query.filter(Recommendation.status == status)
        if direction:
            query = query.filter(Recommendation.direction == direction)
        if held == "yes":
            query = query.filter(Recommendation.ticker_id.in_(list(holdings)))
        elif held == "no":
            query = query.filter(Recommendation.ticker_id.not_in(list(holdings)))
        recs = query.limit(200).all()
        for rec in recs:
            _ = (rec.ticker.symbol, rec.outcome)  # load inside session
        # Latest *stored* close per call: "Entry @ call" is the price at call
        # time, "Last" is what the data says it trades at now (never a guess).
        prices = {rec.id: latest_price(session, rec.ticker_id) for rec in recs}
        board = scoreboard(session).overall
        counts = {
            s: session.query(Recommendation).filter_by(status=s).count()
            for s in ("open", "hit_stop", "hit_target", "closed", "expired", "dismissed")
        }
    return templates.TemplateResponse(
        request=request, name="recommendations.html",
        context={"disclaimer": DISCLAIMER, "recs": recs, "board": board,
                 "counts": counts, "status": status, "direction": direction,
                 "sort": sort, "prices": prices, "holdings": holdings, "held": held,
                 "benchmark": settings.benchmark_symbol},
    )


@router.post("/recommendations/{rec_id}/close")
def close_recommendation(rec_id: int):
    with session_scope() as session:
        rec = session.get(Recommendation, rec_id)
        if rec is not None and rec.status == "open":
            rec.status = "closed"
            rec.closed_at = dt.datetime.now(dt.timezone.utc)
        session.commit()
    return JSONResponse({}, headers={"HX-Redirect": "/recommendations"})


@router.post("/recommendations/{rec_id}/dismiss")
def dismiss_recommendation(rec_id: int):
    """User chose *not* to act on the call - tracked so the scoreboard can
    later answer whether ignoring it was the right call."""
    with session_scope() as session:
        rec = session.get(Recommendation, rec_id)
        if rec is not None and rec.status == "open":
            rec.status = "dismissed"
            rec.closed_at = dt.datetime.now(dt.timezone.utc)
        session.commit()
    return JSONResponse({}, headers={"HX-Redirect": "/recommendations"})
