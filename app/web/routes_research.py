"""Research pages: search, report view, run-new-research."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from sqlalchemy import select

from app.config import get_settings
from app.db.base import session_scope
from app.db.models import ResearchReport, Ticker
from app.ingestion.ingest import refresh_symbol
from app.providers.registry import build_provider
from app.research.engine import RuleBasedAdvisor
from app.research.report import closes_with_dates
from app.web.common import DISCLAIMER, _json_safe, templates

router = APIRouter()


@router.get("/research")
def research_index(request: Request, q: str | None = None):
    with session_scope() as session:
        query = (
            session.query(ResearchReport, Ticker.symbol)
            .join(Ticker, Ticker.id == ResearchReport.ticker_id)
            .order_by(ResearchReport.created_at.desc())
        )
        if q:
            query = query.filter(Ticker.symbol.ilike(f"%{q.strip().upper()}%"))
        reports = query.limit(50).all()
        tickers = session.query(Ticker).order_by(Ticker.symbol).limit(600).all()
    return templates.TemplateResponse(
        request=request, name="research.html",
        context={"disclaimer": DISCLAIMER, "reports": reports, "tickers": tickers, "q": q},
    )


@router.get("/research/{symbol}", response_class=HTMLResponse)
def research_report(request: Request, symbol: str):
    with session_scope() as session:
        ticker = session.scalar(select(Ticker).where(Ticker.symbol == symbol.upper()))
        if ticker is None:
            return templates.TemplateResponse(
                request=request, name="research.html", status_code=404,
                context={"disclaimer": DISCLAIMER, "reports": [], "tickers": [],
                         "q": symbol, "not_found": symbol.upper()},
            )
        report = session.scalars(
            select(ResearchReport).where(ResearchReport.ticker_id == ticker.id)
            .order_by(ResearchReport.created_at.desc()).limit(1)
        ).first()
        bars = closes_with_dates(session, ticker.id)[-500:]
        price_series = [{"date": d.isoformat(), "close": c} for d, c in bars]
    return templates.TemplateResponse(
        request=request, name="report.html",
        context={"disclaimer": DISCLAIMER, "ticker": ticker, "report": report,
                 "price_series": _json_safe(price_series),
                 "price_series_json": _json_safe(price_series)},
    )


@router.post("/research/{symbol}/run")
async def run_research(symbol: str, fetch: bool = Form(True)):
    """Full pipeline for one ticker: fetch -> score -> recommend (HTMX button)."""
    settings = get_settings()
    advisor = RuleBasedAdvisor()
    saved_symbol: str | None = None
    with session_scope() as session:
        if fetch:
            provider = build_provider(settings)
            try:
                refresh_symbol(session, provider, symbol)
            except Exception:
                pass  # score whatever we already have; the report flags staleness
        report = advisor.research(session, symbol)
        if report is not None:
            advisor.recommendations_for(session, report)
            saved_symbol = report.ticker.symbol
        session.commit()
    if saved_symbol is None:
        return JSONResponse({"HX-Redirect": "/research"}, headers={"HX-Redirect": "/research"})
    target = f"/research/{saved_symbol}"
    return JSONResponse({"HX-Redirect": target}, headers={"HX-Redirect": target})
