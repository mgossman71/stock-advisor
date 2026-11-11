"""Screener page: universe table with the five sub-scores as columns."""

from __future__ import annotations

from fastapi import APIRouter, Request
from sqlalchemy import func, select

from app.db.base import session_scope
from app.db.models import ResearchReport, Ticker
from app.background import runner
from app.jobs import run_screen_only
from app.web.routes_jobs import job_status_response
from app.web.common import DISCLAIMER, templates

router = APIRouter()


@router.get("/screen")
def screen_page(request: Request, sector: str | None = None, q: str | None = None):
    """Latest report per ticker joined onto the ticker row (sortable client-side)."""
    with session_scope() as session:
        latest = (
            select(ResearchReport.ticker_id,
                   func.max(ResearchReport.created_at).label("created"))
            .group_by(ResearchReport.ticker_id).subquery()
        )
        rows = session.execute(
            select(Ticker, ResearchReport)
            .outerjoin(latest, latest.c.ticker_id == Ticker.id)
            .outerjoin(ResearchReport,
                       (ResearchReport.ticker_id == latest.c.ticker_id)
                       & (ResearchReport.created_at == latest.c.created))
            .where((Ticker.in_universe.is_(True)) | (Ticker.in_watchlist.is_(True)))
            .order_by(Ticker.symbol)
        ).all()
        sectors = sorted({t.sector for t, _ in rows if t.sector})
        table = []
        for ticker, report in rows:
            if sector and ticker.sector != sector:
                continue
            if q and q.strip().upper() not in ticker.symbol.upper():
                continue
            subs = {}
            score = stance = created = None
            if report is not None:
                subs = {s["name"]: s["score"] for s in report.payload_json.get("sub_scores", [])}
                score, stance, created = report.score, report.overall_stance, report.created_at
            table.append({"ticker": ticker, "report": report, "subs": subs,
                          "score": score, "stance": stance, "created_at": created})
    return templates.TemplateResponse(
        request=request, name="screen.html",
        context={"disclaimer": DISCLAIMER, "rows": table, "sectors": sectors,
                 "sector": sector, "q": q},
    )


@router.post("/screen/run")
def run_screen(request: Request):
    """Score the tracked universe in the background (no fetching - that's the
    pipeline); returns the polling progress panel."""
    runner.start("screen", run_screen_only)
    return job_status_response(request)
