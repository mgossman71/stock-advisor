"""Recommendations: full history, outcomes, alpha scoreboard, manual close."""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.db.base import session_scope
from app.db.models import Recommendation
from app.research.outcomes import scoreboard
from app.web.common import DISCLAIMER, templates

router = APIRouter()


@router.get("/recommendations")
def recommendations_page(request: Request, status: str | None = None,
                         direction: str | None = None, sort: str = "best"):
    settings = get_settings()
    with session_scope() as session:
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
        recs = query.limit(200).all()
        for rec in recs:
            _ = (rec.ticker.symbol, rec.outcome)  # load inside session
        board = scoreboard(session).overall
        counts = {
            s: session.query(Recommendation).filter_by(status=s).count()
            for s in ("open", "hit_stop", "hit_target", "closed", "expired", "dismissed")
        }
    return templates.TemplateResponse(
        request=request, name="recommendations.html",
        context={"disclaimer": DISCLAIMER, "recs": recs, "board": board,
                 "counts": counts, "status": status, "direction": direction,
                 "sort": sort, "benchmark": settings.benchmark_symbol},
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
