"""Settings page: AI analyst (LLM), scoring weights, alert defaults, read-only env."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from app import autorefresh
from app.auth import MIN_PASSWORD_LENGTH
from app.background import runner
from app.config import get_settings
from app.db.base import session_scope
from app.db.models import AppSetting, Order, ScoringWeight
from app.research.llm import (
    LLMClient, LLMError, config_from_form, load_llm_config, save_llm_config,
)
from app.research import scan as scan_config
from app.research.scoring import DEFAULT_WEIGHTS, ensure_default_weights
from app.web.common import DISCLAIMER, templates

router = APIRouter()

EDITABLE_KEYS = ("alert_default_drawdown_pct", "alert_default_earnings_within_days")

#: ``?pw=`` result codes from ``POST /settings/password`` -> (ok, message).
PASSWORD_MESSAGES = {
    "changed": (True, "Password changed. Every other session has been signed out."),
    "wrong": (False, "Current password is wrong."),
    "mismatch": (False, "The new passwords do not match."),
    "short": (False, "The new password is too short."),
    "locked": (False, "Too many failed attempts. Try again in 15 minutes."),
}


@router.get("/settings")
def settings_page(request: Request, pw: str = ""):
    settings = get_settings()
    with session_scope() as session:
        ensure_default_weights(session)
        weights = session.query(ScoringWeight).order_by(ScoringWeight.name).all()
        app_settings = {s.key: s.value for s in session.query(AppSetting)}
        has_orders = session.query(Order).count() > 0
        llm = load_llm_config(session)
        auto = autorefresh.load_config(session)
        scan = scan_config.load_scan_config(session, settings)
        universe = scan_config.universe_info(session)
    return templates.TemplateResponse(
        request=request, name="settings.html",
        context={"disclaimer": DISCLAIMER, "weights": weights,
                 "app_settings": app_settings, "settings": settings,
                 "has_orders": has_orders, "llm": llm, "scan": scan, "universe": universe,
                 "auto": auto,
                 "auto_next": {m: autorefresh.next_run_at(m) for m in autorefresh.MODES},
                 "auto_live": autorefresh.scheduler_running(),
                 "auto_presets": autorefresh.PRESETS,
                 "auto_poll_seconds": int(autorefresh.POLL_SECONDS),
                 "auto_max_wait_hours": int(autorefresh.MAX_WAIT_SECONDS // 3600),
                 "job_now": runner.status(), "job_waiting": runner.waiting(),
                 "pw_message": PASSWORD_MESSAGES.get(pw),
                 "min_password_length": MIN_PASSWORD_LENGTH,
                 "env_summary": {
                     "database": str(settings.db_path or settings.database_url),
                     "provider": settings.market_data_provider,
                     "benchmark": settings.benchmark_symbol,
                     "initial_cash_env": str(settings.initial_cash),
                     "commission_per_share": str(settings.commission_per_share),
                     "slippage_bps": str(settings.slippage_bps),
                     "short_borrow_apy": str(settings.short_borrow_apy),
                     "keys": {
                         "finnhub": bool(settings.finnhub_api_key),
                         "alphavantage": bool(settings.alphavantage_api_key),
                         "polygon": bool(settings.polygon_api_key),
                     },
                 }},
    )


@router.post("/settings/weights")
async def save_weights(request: Request):
    """Accepts weight_<name> form fields; validates they sum to 1.0."""
    form = await request.form()
    with session_scope() as session:
        incoming: dict[str, Decimal] = {}
        for name in DEFAULT_WEIGHTS:
            raw = form.get(f"weight_{name}")
            if raw is None:
                continue
            try:
                incoming[name] = Decimal(str(raw))
            except InvalidOperation:
                return RedirectResponse("/settings", status_code=303)
        if len(incoming) == len(DEFAULT_WEIGHTS) and sum(incoming.values()) == Decimal("1"):
            for name, weight in incoming.items():
                row = session.query(ScoringWeight).filter_by(name=name).first()
                if row is not None:
                    row.weight = weight
                    row.updated_at = dt.datetime.now(dt.timezone.utc)
        session.commit()
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/defaults")
def save_defaults(alert_default_drawdown_pct: str = Form(""),
                  alert_default_earnings_within_days: str = Form("")):
    with session_scope() as session:
        for key, raw in (("alert_default_drawdown_pct", alert_default_drawdown_pct),
                         ("alert_default_earnings_within_days",
                          alert_default_earnings_within_days)):
            if not raw:
                continue
            try:
                Decimal(raw)
            except InvalidOperation:
                continue
            row = session.query(AppSetting).filter_by(key=key).first()
            if row is None:
                row = AppSetting(key=key, value=raw)
                session.add(row)
            row.value = raw
            row.updated_at = dt.datetime.now(dt.timezone.utc)
        session.commit()
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/autorefresh")
async def save_autorefresh(request: Request):
    """Persist both timers and re-arm the live scheduler (no restart needed).

    Fields per job (``prices`` / ``full``): ``<job>_enabled``,
    ``<job>_minutes``, ``<job>_market_hours_only``.  Unticked boxes are absent.
    """
    form = await request.form()
    with session_scope() as session:
        current = autorefresh.load_config(session)
        schedules = {}
        for mode in autorefresh.MODES:
            schedules[mode] = autorefresh.Schedule(
                enabled=form.get(f"{mode}_enabled") == "1",
                minutes=autorefresh.parse_minutes(
                    form.get(f"{mode}_minutes"), current.for_mode(mode).minutes,
                    autorefresh.MIN_MINUTES_BY_MODE[mode]),
                market_hours_only=form.get(f"{mode}_market_hours_only") == "1",
            )
        config = autorefresh.AutoRefreshConfig(**schedules)
        autorefresh.save_config(session, config)
        session.commit()
    autorefresh.apply_schedule(config)
    return RedirectResponse("/settings#autorefresh", status_code=303)


@router.post("/settings/scan")
async def save_scan(request: Request):
    """Scan floors + shortlist size; a changed floor rebuilds the stock list
    on the next full refresh."""
    form = await request.form()
    with session_scope() as session:
        current = scan_config.load_scan_config(session)
        scan_config.save_scan_config(session, scan_config.config_from_form(form, current))
        session.commit()
    return RedirectResponse("/settings#discovery", status_code=303)


@router.post("/settings/llm")
async def save_llm(request: Request):
    form = await request.form()
    with session_scope() as session:
        save_llm_config(session, config_from_form(dict(form), load_llm_config(session)))
        session.commit()
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/llm/test")
async def test_llm(request: Request):
    """HTMX: probe the endpoint as currently typed in the form (saved or not)."""
    form = await request.form()
    with session_scope() as session:
        config = config_from_form(dict(form), load_llm_config(session))
    context: dict = {"ok": False, "models": [], "model": config.model, "reply": None,
                     "error": None, "models_error": None}
    if not config.base_url:
        context["error"] = "Enter a base URL first."
    else:
        client = LLMClient(config)
        try:
            # Some servers (older llama.cpp) have no /models; chat decides.
            try:
                context["models"] = client.list_models()
            except LLMError as exc:
                context["models_error"] = str(exc)
            if config.model:
                context["reply"] = client.chat(
                    [{"role": "user", "content": 'Reply with exactly {"ok": true}'}],
                )[:200]
                context["ok"] = True
            else:
                context["ok"] = context["models_error"] is None
                context["error"] = context["models_error"]
        except LLMError as exc:
            context["error"] = str(exc)
        finally:
            client.close()
    return templates.TemplateResponse(request=request, name="partials/llm_test.html",
                                      context=context)
