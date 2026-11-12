"""Screener page: the discovery scan over the whole US-listed universe.

Shows the latest ``scan_scores`` in rank order, which names made the
shortlist (and so got the full fetch + report + AI), the emerging-lens score
(``app/research/emerging.py``) and the full report's verdict where one
exists.  Filters: symbol/name, sector, size band, held, shortlist only,
emerging candidates only; sortable by any sub-score or the lens.
"""

from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Request
from sqlalchemy import func, select

from app.background import runner
from app.db.base import session_scope
from app.db.models import ResearchReport, ScanScore, Ticker
from app.jobs import run_screen_only
from app.research import emerging
from app.research.scan import latest_scan_date, load_scan_config, universe_info
from app.web.common import DISCLAIMER, templates
from app.web.routes_jobs import job_status_response
from app.web.routes_recommendations import holdings_by_ticker

router = APIRouter()

BLOCKS = ("value", "momentum", "quality", "risk")
#: (key, label, upper bound in $) - "small" < $2B <= "mid" < $10B <= "large".
CAP_BANDS = (("small", "small cap (< $2B)", Decimal("2e9")),
             ("mid", "mid cap ($2B–$10B)", Decimal("10e9")),
             ("large", "large cap (≥ $10B)", None))
SORTS = ("rank", "emerging") + BLOCKS
PAGE_ROWS = 200


def cap_band(market_cap: Decimal | None) -> str | None:
    if market_cap is None:
        return None
    if market_cap < Decimal("2e9"):
        return "small"
    return "mid" if market_cap < Decimal("10e9") else "large"


def cap_text(market_cap: Decimal | None) -> str:
    """$1.2T / $34.5B / $612M - never 0 for unknown."""
    if market_cap is None:
        return "—"
    value = float(market_cap)
    if value >= 1e12:
        return f"${value / 1e12:.1f}T"
    if value >= 1e9:
        return f"${value / 1e9:.1f}B"
    return f"${value / 1e6:.0f}M"


@router.get("/screen")
def screen_page(request: Request, sector: str | None = None, q: str | None = None,
                cap: str = "", held: str = "", view: str = "", sort: str = "rank",
                all: int = 0):
    sort = sort if sort in SORTS else "rank"
    with session_scope() as session:
        scan_date = latest_scan_date(session)
        config = load_scan_config(session)
        info = universe_info(session)
        holdings = holdings_by_ticker(session)
        universe_count = session.scalar(
            select(func.count(Ticker.id)).where(Ticker.in_universe.is_(True))) or 0
        rows = session.execute(
            select(ScanScore, Ticker).join(Ticker, Ticker.id == ScanScore.ticker_id)
            .where(ScanScore.scan_date == scan_date).order_by(ScanScore.rank)
        ).all() if scan_date else []
        sectors = sorted({t.sector for _, t in rows if t.sector})
        with_fundamentals = sum(1 for s, _ in rows if s.has_fundamentals)

        needle = (q or "").strip().upper()
        table = []
        for scan, ticker in rows:
            shortlisted = scan.rank is not None and scan.rank <= config.shortlist_size
            lens_pick = (scan.emerging_rank is not None and scan.emerging_score is not None
                         and scan.emerging_score >= emerging.SHORTLIST_MIN_SCORE
                         and scan.emerging_rank <= config.emerging_size)
            if sector and ticker.sector != sector:
                continue
            if needle and needle not in ticker.symbol and needle not in (ticker.name or "").upper():
                continue
            if cap and cap_band(ticker.market_cap) != cap:
                continue
            if held == "yes" and ticker.id not in holdings:
                continue
            if held == "no" and ticker.id in holdings:
                continue
            if view == "shortlist" and not shortlisted:
                continue
            if view == "emerging" and not lens_pick:
                continue
            subs = {name: (Decimal(scan.sub_scores[name])
                           if scan.sub_scores.get(name) is not None else None)
                    for name in BLOCKS}
            table.append({"scan": scan, "ticker": ticker, "subs": subs,
                          "shortlisted": shortlisted, "lens_pick": lens_pick,
                          "reasons": (scan.emerging or {}).get("reasons", []),
                          "cap": cap_text(ticker.market_cap),
                          "held": holdings.get(ticker.id)})
        if sort == "emerging":
            table.sort(key=lambda r: (r["scan"].emerging_score is None,
                                      -(r["scan"].emerging_score or 0)))
        elif sort != "rank":
            table.sort(key=lambda r: (r["subs"][sort] is None, -(r["subs"][sort] or 0)))
        matched = len(table)
        if not all:
            table = table[:PAGE_ROWS]

        # The full report's verdict for the rows on screen (shortlist + held).
        ids = [r["ticker"].id for r in table]
        latest = (select(ResearchReport.ticker_id,
                         func.max(ResearchReport.created_at).label("created"))
                  .where(ResearchReport.ticker_id.in_(ids))
                  .group_by(ResearchReport.ticker_id).subquery())
        reports = {r.ticker_id: r for r in session.scalars(
            select(ResearchReport).join(
                latest, (ResearchReport.ticker_id == latest.c.ticker_id)
                & (ResearchReport.created_at == latest.c.created)))} if ids else {}
        for row in table:
            row["report"] = reports.get(row["ticker"].id)

    return templates.TemplateResponse(
        request=request, name="screen.html",
        context={"disclaimer": DISCLAIMER, "rows": table, "sectors": sectors,
                 "sector": sector, "q": q, "cap": cap, "held": held, "view": view,
                 "sort": sort, "all": all, "matched": matched, "page_rows": PAGE_ROWS,
                 "scan_date": scan_date, "scanned": len(rows),
                 "with_fundamentals": with_fundamentals, "universe_count": universe_count,
                 "info": info, "config": config, "cap_bands": CAP_BANDS, "blocks": BLOCKS},
    )


@router.post("/screen/run")
def run_screen(request: Request):
    """Re-scan stored data and fully research the shortlist in the background
    (no fetching - that's the pipeline); returns the polling progress panel."""
    runner.start("screen", run_screen_only)
    return job_status_response(request)
