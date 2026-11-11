"""Manual job trigger, job progress + data export.

`POST /jobs/prices` starts the quick price refresh (quotes for what the user
holds or watches only); `POST /jobs/refresh` starts the full nightly pass in the background (via
``app.background.runner``) and returns a progress panel that polls
`GET /jobs/status` until the job finishes - no request stays open for the
length of the job, so a reverse proxy can't time it out.
"""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from sqlalchemy import inspect
from sqlalchemy import text as sa_text

from app.background import runner
from app.db.base import session_scope
from app.jobs import run_pipeline, run_price_refresh
from app.web.common import templates

router = APIRouter()


def job_status_response(request: Request, *, watching: bool = True):
    return templates.TemplateResponse(
        request=request, name="partials/job_status.html",
        context={"status": runner.status(), "watching": watching},
    )


@router.post("/jobs/refresh")
def jobs_refresh(request: Request, max_symbols: int | None = Form(None),
                 with_alternatives: bool = Form(True)):
    """Start the pipeline (or, if a job is already running, show its progress).

    News, analyst actions and earnings are fetched by default, like the
    nightly pass - the AI analyst's screen reads them, and without this a
    manual Full refresh judged fresh prices against stale news.
    ``with_alternatives=0`` opts out.
    """
    runner.start("refresh", lambda progress: run_pipeline(
        progress, max_symbols=max_symbols, with_alternatives=bool(with_alternatives)))
    return job_status_response(request)


@router.post("/jobs/prices")
def jobs_prices(request: Request):
    """Quick job: live quotes for held/watched symbols, then marks + alerts."""
    runner.start("prices", run_price_refresh)
    return job_status_response(request)


@router.get("/jobs/status")
def jobs_status(request: Request, watching: bool = False):
    """Progress panel.  ``watching`` = polled after a start, so show the result
    when the job ends; a plain page load shows nothing once it's idle."""
    return job_status_response(request, watching=watching)


@router.get("/api/export.json")
def export_json():
    """Full relational dump: every table, every row (the spec's export hook)."""
    with session_scope() as session:
        engine = session.get_bind()
        names = sorted(inspect(engine).get_table_names())
        payload: dict[str, list[dict]] = {}
        for name in names:
            if name == "alembic_version":
                continue
            rows = session.execute(sa_text(f'SELECT * FROM "{name}"')).mappings().all()
            payload[name] = [{k: (str(v) if v is not None else None) for k, v in dict(r).items()}
                             for r in rows]
    return payload
