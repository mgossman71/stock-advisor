"""Manual job trigger + data export.

`POST /jobs/refresh` runs the full nightly pass on demand (acceptance
criterion #4).  It is defined as a sync ``def`` so FastAPI runs it in the
thread pool and the event loop keeps serving the UI while providers chatter.
"""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from sqlalchemy import inspect
from sqlalchemy import text as sa_text

from app.config import get_settings
from app.db.base import session_scope
from app.jobs import run_nightly
from app.providers.registry import build_provider
from app.web.common import DISCLAIMER, templates

router = APIRouter()


@router.post("/jobs/refresh")
def jobs_refresh(request: Request, max_symbols: int | None = Form(None),
                 with_alternatives: bool = Form(False)):
    settings = get_settings()
    provider = build_provider(settings)
    with session_scope() as session:
        report = run_nightly(session, provider, settings=settings,
                             max_symbols=max_symbols,
                             with_alternatives=bool(with_alternatives))
    return templates.TemplateResponse(
        request=request, name="partials/jobs_result.html",
        context={"disclaimer": DISCLAIMER, "report": report},
    )


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
