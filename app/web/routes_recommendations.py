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
                         direction: str | None = None):
    settings = get_settings()
    with session_scope() as session:
        query = session.query(Recommendation).order_by(Recommendation.generated_at.desc())
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
            for s in ("open", "hit_stop", "hit_target", "closed", "expired")
        }
    return templates.TemplateResponse(
        request=request, name="recommendations.html",
        context={"disclaimer": DISCLAIMER, "recs": recs, "board": board,
                 "counts": counts, "status": status, "direction": direction,
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
