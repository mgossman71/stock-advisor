"""Minimal FastAPI entry point so the container has something to serve.

Phase 5 replaces this with the real UI (HTMX pages, charts, portfolio screens).
Everything here is read-only status: what the app is configured to do, and what
is actually in the database.  No scoring, no orders, no charts yet.
"""
from __future__ import annotations

import datetime as dt

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text

from app.config import BASE_DIR, get_settings
from app.db.base import session_scope
from app.ingestion.stats import coverage_by_ticker, recent_fetch_log, table_counts, table_names
from app.providers.registry import build_provider, provider_names

templates = Jinja2Templates(directory=str(BASE_DIR / "web" / "templates"))

app = FastAPI(
    title=get_settings().app_name,
    version="0.1.0",
    description="Research + paper-trading app. Phase 1: data layer only.",
)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    """Liveness: the process is up and the database answers."""
    try:
        with session_scope() as session:
            session.execute(text("SELECT 1"))
    except Exception as exc:  # pragma: no cover - depends on a broken volume/URL
        raise HTTPException(status_code=503, detail=f"database unavailable: {exc}") from exc
    return {"status": "ok"}


@app.get("/api/status")
def status() -> dict[str, object]:
    """Machine-readable status: config, row counts, coverage, provider log."""
    return status_payload()


@app.get("/", response_class=HTMLResponse)
def status_page(request: Request) -> HTMLResponse:
    """Human-readable status page; the real dashboard is Phase 5/6."""
    return templates.TemplateResponse(
        request=request, name="status.html", context=status_payload()
    )


def status_payload() -> dict[str, object]:
    """Assemble the status report once, for both the HTML and JSON endpoints."""
    settings = get_settings()
    provider_chain = list(provider_names(build_provider(settings)))
    with session_scope() as session:
        counts = table_counts(session)
        coverage = coverage_by_ticker(session)
        fetch_log = recent_fetch_log(session, limit=10)
        tables = len([t for t in table_names(session) if t != "alembic_version"])
    return {
        "app": settings.app_name,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "database": str(settings.db_path or settings.database_url),
        "tables": tables,
        "market_data_provider": settings.market_data_provider,
        "provider_chain": provider_chain,
        "benchmark": settings.benchmark_symbol,
        "counts": counts,
        "coverage": coverage,
        "fetch_log": fetch_log,
    }
