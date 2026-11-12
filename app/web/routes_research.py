"""Research pages: search, report view, run-new-research.

Search covers every stored ticker (symbol or company name), not just the
ones with reports - the discovery funnel only reports on its shortlist, so
most stocks have no report until you ask for one.  A valid symbol the app
does not track yet can be researched from here too: ``run_research``
fetches it first, and refuses to keep a ticker the providers know nothing
about.
"""

from __future__ import annotations

import re
from urllib.parse import quote

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from sqlalchemy import func, or_, select

from app.config import get_settings
from app.db.base import session_scope
from app.db.models import PriceHistory, ResearchReport, Ticker
from app.ingestion.ingest import ingest_quote, refresh_symbol
from app.providers.registry import build_provider
from app.research.llm_advisor import advisor_for
from app.research.report import closes_with_dates
from app.web.common import DISCLAIMER, _json_safe, templates
from app.web.display import (
    RANGES, load_default_range, needs_live_quote, price_now, report_price, slice_range,
    valid_range,
)

router = APIRouter()


#: Ticker symbols as the providers spell them: AAPL, BRK-B, ^GSPC, BF.B.
SYMBOL_RE = re.compile(r"^\^?[A-Z][A-Z0-9.\-]{0,9}$")

ERRORS = {
    "nodata": "No price data found for {sym} - check the symbol (US listings only).",
    "badsymbol": "“{sym}” isn't a valid ticker symbol.",
}


def _search(session, q: str) -> list[dict]:
    """Tickers whose symbol starts with / name contains ``q``, exact match first,
    each with its latest report (or None)."""
    needle = q.strip()
    rows = session.scalars(
        select(Ticker).where(or_(Ticker.symbol.ilike(f"{needle}%"),
                                 Ticker.name.ilike(f"%{needle}%")))
        .order_by((Ticker.symbol == needle.upper()).desc(), Ticker.symbol).limit(25)
    ).all()
    matches = []
    for ticker in rows:
        report = session.scalars(
            select(ResearchReport).where(ResearchReport.ticker_id == ticker.id)
            .order_by(ResearchReport.created_at.desc()).limit(1)
        ).first()
        matches.append({"ticker": ticker, "report": report})
    return matches


def _research_page(request: Request, *, q: str | None = None, err: str | None = None,
                   not_found: str | None = None, status_code: int = 200):
    q = (q or "").strip() or None
    with session_scope() as session:
        # One row per stock: its newest report (re-running research must not
        # list the stock twice).  Highest id = newest, without timestamp ties.
        newest = (select(func.max(ResearchReport.id).label("id"))
                  .group_by(ResearchReport.ticker_id).subquery())
        reports = (
            session.query(ResearchReport, Ticker.symbol)
            .join(Ticker, Ticker.id == ResearchReport.ticker_id)
            .join(newest, newest.c.id == ResearchReport.id)
            .order_by(ResearchReport.created_at.desc(), ResearchReport.id.desc())
            .limit(50).all()
        )
        matches = _search(session, q) if q else []
        for m in matches:  # touch lazy attrs inside the session
            _ = (m["ticker"].symbol, m["ticker"].name)
            m["now"] = price_now(session, m["ticker"].id)
        prices = {report.ticker_id: price_now(session, report.ticker_id)
                  for report, _symbol in reports}
    candidate = (not_found or q or "").upper()
    exact = any(m["ticker"].symbol == candidate for m in matches)
    return templates.TemplateResponse(
        request=request, name="research.html", status_code=status_code,
        context={"disclaimer": DISCLAIMER, "reports": reports, "q": q, "prices": prices,
                 "matches": matches, "not_found": not_found,
                 # Offer "research it now" for a plausible symbol we don't hold.
                 "can_add": candidate if SYMBOL_RE.match(candidate) and not exact else None,
                 "error": ERRORS[err].format(sym=candidate) if err in ERRORS else None},
    )


@router.get("/research")
def research_index(request: Request, q: str | None = None, err: str | None = None):
    return _research_page(request, q=q, err=err)


@router.get("/research/{symbol}", response_class=HTMLResponse)
def research_report(request: Request, symbol: str,
                    range_: str | None = Query(None, alias="range")):
    """One stock's report.  The chart opens at the global default range
    (Settings -> Display); ``?range=`` overrides it for this view only."""
    with session_scope() as session:
        ticker = session.scalar(select(Ticker).where(Ticker.symbol == symbol.upper()))
    if ticker is None:
        return _research_page(request, not_found=symbol.upper(), status_code=404)
    with session_scope() as session:
        report = session.scalars(
            select(ResearchReport).where(ResearchReport.ticker_id == ticker.id)
            .order_by(ResearchReport.created_at.desc()).limit(1)
        ).first()
        now = price_now(session, ticker.id)
        default_range = load_default_range(session)
        chart_range = valid_range(range_) or default_range
        all_bars = closes_with_dates(session, ticker.id)
        bars = slice_range(all_bars, chart_range)
        price_series = [{"date": d.isoformat(), "close": c} for d, c in bars]
    return templates.TemplateResponse(
        request=request, name="report.html",
        context={"disclaimer": DISCLAIMER, "ticker": ticker, "report": report,
                 "price_series": _json_safe(price_series),
                 "price_series_json": _json_safe(price_series),
                 "chart_range": chart_range, "default_range": default_range,
                 "now": now, "report_price": _moved(report, now),
                 "auto_refresh": needs_live_quote(now),
                 "ranges": RANGES,
                 "history_from": all_bars[0][0] if all_bars else None},
    )


def quote_provider(settings):
    """Provider for on-page live quotes (tests override this, like
    ``routes_portfolio.order_provider``)."""
    return build_provider(settings)


@router.get("/research/{symbol}/price")
def live_price(request: Request, symbol: str, force: int = 0):
    """The stock page's price block, with a live quote fetched first when the
    stored price is stale (or ``force=1`` from the refresh button).  The quote
    is stored, so every page - and "Last price" in the lists - sees it."""
    error = None
    with session_scope() as session:
        ticker = session.scalar(select(Ticker).where(Ticker.symbol == symbol.upper()))
        if ticker is None:
            return HTMLResponse("", status_code=404)
        now = price_now(session, ticker.id)
        if force or needs_live_quote(now):
            try:
                stats, quote = ingest_quote(session, quote_provider(get_settings()),
                                            ticker.symbol,
                                            asset_class=ticker.asset_class or "stock")
                session.commit()
                if quote is None:
                    error = "; ".join(stats.notes) or "no quote"
            except Exception as exc:  # a provider hiccup must not break the page
                session.rollback()
                error = type(exc).__name__
            now = price_now(session, ticker.id)
        report = session.scalars(
            select(ResearchReport).where(ResearchReport.ticker_id == ticker.id)
            .order_by(ResearchReport.id.desc()).limit(1)).first()
        moved = _moved(report, now)
    return templates.TemplateResponse(
        request=request, name="partials/price_now.html",
        context={"ticker": ticker, "now": now, "report_price": moved,
                 "auto_refresh": False, "quote_error": error},
    )


def _moved(report: ResearchReport | None, now) -> dict | None:
    """The price a report was written at and how far the stock has moved
    since - reports are snapshots, the price above them is live."""
    if report is None or now is None:
        return None
    written = report_price(report)
    if written is None or not written[0]:
        return None
    price, as_of = written
    return {"price": price, "as_of": as_of, "moved_pct": (now.price / price - 1) * 100}


def _hx_redirect(target: str) -> JSONResponse:
    return JSONResponse({"HX-Redirect": target}, headers={"HX-Redirect": target})


@router.post("/research/{symbol}/run")
def run_research(symbol: str, fetch: bool = Form(True)):
    """Full pipeline for one ticker: fetch -> score -> recommend (HTMX button).

    A sync ``def`` on purpose: FastAPI runs it in the thread pool, so a slow
    fetch or LLM call doesn't freeze every other page (as ``async def`` with
    blocking calls did).  Works for tickers not stored yet; if the providers
    return no prices at all, nothing is kept.
    """
    symbol = symbol.strip().upper()
    if not SYMBOL_RE.match(symbol):
        return _hx_redirect(f"/research?q={quote(symbol)}&err=badsymbol")
    settings = get_settings()
    saved_symbol: str | None = None
    with session_scope() as session:
        advisor = advisor_for(session)
        if fetch:
            provider = build_provider(settings)
            try:
                refresh_symbol(session, provider, symbol)
            except Exception:
                pass  # score whatever we already have; the report flags staleness
        has_prices = session.scalar(
            select(func.count(PriceHistory.id)).join(Ticker, Ticker.id == PriceHistory.ticker_id)
            .where(Ticker.symbol == symbol)
        )
        if not has_prices:
            session.rollback()  # don't keep a ticker nobody has data for
            return _hx_redirect(f"/research?q={quote(symbol)}&err=nodata")
        report = advisor.research(session, symbol)
        if report is not None:
            advisor.recommendations_for(session, report)
            saved_symbol = report.ticker.symbol
        session.commit()
    if saved_symbol is None:
        return _hx_redirect("/research")
    return _hx_redirect(f"/research/{saved_symbol}")
