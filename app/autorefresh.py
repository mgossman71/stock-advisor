"""Interval auto-refresh: the "Run refresh job" button, on a timer.

Config lives in ``app_settings`` rows ``autorefresh_*`` (edited on the Settings
page), not env vars, so it can be changed without a restart: saving calls
``apply_schedule``, which adds, re-times or removes the ``autorefresh`` job on
the scheduler the web process armed at startup.  The nightly cron is separate
and unaffected.

Each tick goes through the single-flight ``runner`` like every other long job,
so a tick that lands while a refresh (or the nightly run) is still going is
skipped rather than queued - an interval shorter than one pipeline pass can't
pile up work.  "Market hours only" means Mon-Fri 09:30-16:15 New York time
(the extra 15 minutes catch the closing print); like ``app/trading/freshness``
there is deliberately no holiday calendar.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy.orm import Session

from app.background import runner
from app.db.models import AppSetting

logger = logging.getLogger("stock-advisor")

JOB_ID = "autorefresh"
JOB_NAME = "auto-refresh"
MIN_MINUTES = 5
MAX_MINUTES = 24 * 60
DEFAULT_MINUTES = 60

NEW_YORK = ZoneInfo("America/New_York")
MARKET_OPEN = dt.time(9, 30)
MARKET_CLOSE = dt.time(16, 15)

KEYS = {
    "enabled": "autorefresh_enabled",
    "minutes": "autorefresh_minutes",
    "market_hours_only": "autorefresh_market_hours_only",
}

#: The web process's scheduler, set in ``app.main`` at startup (``None`` in
#: tests, CLI and one-shot containers - saving the config then only persists).
_scheduler = None


@dataclass(frozen=True)
class AutoRefreshConfig:
    enabled: bool = False
    minutes: int = DEFAULT_MINUTES
    market_hours_only: bool = True


def parse_minutes(raw: str | None, default: int = DEFAULT_MINUTES) -> int:
    """Clamp to [MIN_MINUTES, MAX_MINUTES]; garbage falls back to ``default``."""
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return max(MIN_MINUTES, min(MAX_MINUTES, value))


def load_config(session: Session) -> AutoRefreshConfig:
    rows = {s.key: s.value for s in session.query(AppSetting)
            .filter(AppSetting.key.in_(KEYS.values()))}
    return AutoRefreshConfig(
        enabled=rows.get(KEYS["enabled"]) == "1",
        minutes=parse_minutes(rows.get(KEYS["minutes"])),
        market_hours_only=rows.get(KEYS["market_hours_only"], "1") == "1",
    )


def save_config(session: Session, config: AutoRefreshConfig) -> None:
    """Upsert every field (caller commits)."""
    values = {
        "enabled": "1" if config.enabled else "0",
        "minutes": str(parse_minutes(str(config.minutes))),
        "market_hours_only": "1" if config.market_hours_only else "0",
    }
    now = dt.datetime.now(dt.timezone.utc)
    for field, value in values.items():
        row = session.query(AppSetting).filter_by(key=KEYS[field]).first()
        if row is None:
            row = AppSetting(key=KEYS[field], value=value)
            session.add(row)
        row.value = value
        row.updated_at = now
    session.flush()


def market_open(now: dt.datetime | None = None) -> bool:
    """Mon-Fri 09:30-16:15 New York time (no holiday calendar on purpose)."""
    local = (now or dt.datetime.now(dt.timezone.utc)).astimezone(NEW_YORK)
    return local.weekday() < 5 and MARKET_OPEN <= local.time() <= MARKET_CLOSE


def run_tick(market_hours_only: bool, *, now: dt.datetime | None = None,
             job=None) -> str:
    """One scheduled tick: ``'ran'``, ``'closed'`` or ``'busy'``."""
    if market_hours_only and not market_open(now):
        return "closed"
    if job is None:
        from app.jobs import run_pipeline  # jobs imports heavy research modules
        job = run_pipeline
    if not runner.run_blocking(JOB_NAME, job):
        logger.info("auto-refresh skipped: another job is still running")
        return "busy"
    return "ran"


def set_scheduler(scheduler) -> None:
    global _scheduler
    _scheduler = scheduler


def apply_schedule(config: AutoRefreshConfig, scheduler=None) -> None:
    """Add, re-time or remove the interval job to match ``config``."""
    scheduler = scheduler if scheduler is not None else _scheduler
    if scheduler is None:
        return
    if not config.enabled:
        if scheduler.get_job(JOB_ID) is not None:
            scheduler.remove_job(JOB_ID)
        logger.info("auto-refresh off")
        return
    scheduler.add_job(run_tick, IntervalTrigger(minutes=config.minutes),
                      kwargs={"market_hours_only": config.market_hours_only},
                      id=JOB_ID, replace_existing=True,
                      max_instances=1, coalesce=True)
    logger.info("auto-refresh every %d min%s", config.minutes,
                " (market hours only)" if config.market_hours_only else "")


def next_run_at() -> dt.datetime | None:
    """When the armed job fires next (``None`` if off or no scheduler)."""
    if _scheduler is None:
        return None
    job = _scheduler.get_job(JOB_ID)
    return job.next_run_time if job is not None else None


def scheduler_running() -> bool:
    return _scheduler is not None
