"""FastAPI entry point: real UI (Phases 3-5) + scheduler + healthcheck.

Lifespan starts APScheduler only when ``SCHEDULER_ENABLED`` is set and the
process is the web server (one-shot CLI/container jobs set it to 0), so two
processes never race on the same SQLite file.  The same process runs the
first-run setup (``AUTO_SETUP``): on a fresh database it loads the universe,
fetches prices and screens in the background, with progress in the UI.  The nightly cron mirrors
``NIGHTLY_CRON`` ("minute hour dom month dow").
"""

from __future__ import annotations

import datetime as dt
import logging
from urllib.parse import quote, urlsplit

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, text

from app import auth, autorefresh
from app.config import BASE_DIR, get_settings
from app.db.base import session_scope
from app.ingestion.stats import coverage_by_ticker, recent_fetch_log, table_counts
from app.background import runner
from app.jobs import needs_first_run, run_pipeline
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

_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


@app.middleware("http")
async def same_origin_writes(request: Request, call_next):
    """Refuse state-changing requests sent from another site (CSRF guard).

    The session cookie alone would let any web page the user visits POST
    to the app and record trades.  Browsers stamp cross-site
    requests with ``Origin``/``Sec-Fetch-Site``; requests with neither (curl,
    the test client, the CLI) are not browser-forged and pass.
    """
    if request.method in _UNSAFE_METHODS:
        host = request.headers.get("host", "")
        origin = request.headers.get("origin") or request.headers.get("referer")
        fetch_site = request.headers.get("sec-fetch-site")
        if fetch_site == "cross-site" or (
            origin and origin != "null" and urlsplit(origin).netloc != host
        ):
            return PlainTextResponse("cross-site request refused", status_code=403)
        if origin == "null":
            return PlainTextResponse("cross-site request refused", status_code=403)
    return await call_next(request)


_PUBLIC_PATHS = {"/login", "/setup", "/healthz"}


@app.middleware("http")
async def require_login(request: Request, call_next):
    """Every route needs a valid session cookie except login/setup, healthz, static.

    Registered after ``same_origin_writes`` so it runs first.  Fails closed:
    no password set -> nothing but the login page (which says how to set one).
    """
    path = request.url.path
    if path in _PUBLIC_PATHS or path.startswith("/static/"):
        return await call_next(request)
    with session_scope() as session:
        _password_hash, secret = auth.load_auth(session)
    if auth.check_token(request.cookies.get(auth.COOKIE_NAME), secret):
        return await call_next(request)
    target = path + (f"?{request.url.query}" if request.url.query else "")
    login = f"/login?next={quote(target, safe='/')}"
    if path.startswith("/api/"):
        return JSONResponse({"detail": "login required"}, status_code=401)
    if request.headers.get("hx-request"):
        return PlainTextResponse("login required", status_code=401,
                                 headers={"HX-Redirect": login})
    return RedirectResponse(login, status_code=303)


def _cron_trigger(expression: str) -> CronTrigger:
    minute, hour, dom, month, dow = expression.split()
    return CronTrigger(minute=minute, hour=hour, day=dom, month=month, day_of_week=dow)


def nightly_job() -> None:
    """APScheduler entry, through the single-flight runner (skips if busy)."""
    if not runner.run_blocking("nightly", run_pipeline):
        logger.info("nightly job skipped: another job is still running")


@app.on_event("startup")
def start_scheduler() -> None:
    settings = get_settings()
    if not settings.scheduler_enabled:
        return
    if settings.auto_setup:
        with session_scope() as session:
            fresh = needs_first_run(session)
        if fresh:
            logger.info("fresh install: loading the universe and prices in the background")
            runner.start("first-run setup", run_pipeline)
    scheduler = BackgroundScheduler(timezone="UTC", daemon=True)
    scheduler.add_job(nightly_job, _cron_trigger(settings.nightly_cron),
                      id="nightly", replace_existing=True)
    scheduler.start()
    app.state.scheduler = scheduler
    autorefresh.set_scheduler(scheduler)
    with session_scope() as session:
        autorefresh.apply_schedule(autorefresh.load_config(session))
    logger.info("scheduler armed: nightly cron %s UTC", settings.nightly_cron)


@app.on_event("shutdown")
def stop_scheduler() -> None:
    scheduler = getattr(app.state, "scheduler", None)
    if scheduler is not None:
        autorefresh.set_scheduler(None)
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
