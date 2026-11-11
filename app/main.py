"""FastAPI entry point: real UI (Phases 3-5) + scheduler + healthcheck.

Lifespan starts APScheduler only when ``SCHEDULER_ENABLED`` is set and the
process is the web server (one-shot CLI/container jobs set it to 0), so two
processes never race on the same SQLite file.  The nightly cron mirrors
``NIGHTLY_CRON`` ("minute hour dom month dow").
"""

from __future__ import annotations

import datetime as dt
import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, text

from app.config import BASE_DIR, get_settings
from app.db.base import session_scope
from app.ingestion.stats import coverage_by_ticker, recent_fetch_log, table_counts
from app.jobs import run_nightly
from app.providers.registry import build_provider
from app.web import routers
from app.web.common import DISCLAIMER, templates

logger = logging.getLogger("stock-advisor")

app = FastAPI(
    title=get_settings().app_name,
    version="1.0.0",
    description="Research + recommendations + paper trading. Educational simulation only.",
)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "web" / "static")), name="static")

for router in routers:
    app.include_router(router)


def _cron_trigger(expression: str) -> CronTrigger:
    minute, hour, dom, month, dow = expression.split()
    return CronTrigger(minute=minute, hour=hour, day=dom, month=month, day_of_week=dow)


def nightly_job() -> None:
    """APScheduler entry: fresh provider + session per run (no leaked state)."""
    settings = get_settings()
    try:
        with session_scope() as session:
            run_nightly(session, build_provider(settings), settings=settings,
                        with_alternatives=True)
    except Exception:  # pragma: no cover - scheduler must never kill the process
        logger.exception("nightly job failed")


@app.on_event("startup")
def start_scheduler() -> None:
    settings = get_settings()
    if not settings.scheduler_enabled:
        return
    scheduler = BackgroundScheduler(timezone="UTC", daemon=True)
    scheduler.add_job(nightly_job, _cron_trigger(settings.nightly_cron),
                      id="nightly", replace_existing=True)
    scheduler.start()
    app.state.scheduler = scheduler
    logger.info("scheduler armed: nightly cron %s UTC", settings.nightly_cron)


@app.on_event("shutdown")
def stop_scheduler() -> None:
    scheduler = getattr(app.state, "scheduler", None)
    if scheduler is not None:
        scheduler.shutdown(wait=False)


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
    settings = get_settings()
    with session_scope() as session:
        counts = table_counts(session)
        coverage = coverage_by_ticker(session)
        fetch_log = recent_fetch_log(session, limit=10)
    return {
        "app": settings.app_name,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "database": str(settings.db_path or settings.database_url),
        "market_data_provider": settings.market_data_provider,
        "benchmark": settings.benchmark_symbol,
        "disclaimer": DISCLAIMER,
        "counts": counts,
        "coverage": coverage,
        "fetch_log": fetch_log,
    }
