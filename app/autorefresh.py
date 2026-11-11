"""Interval auto-refresh: two independent timers, one per refresh job.

* ``prices`` - the "Refresh prices" button (quotes for held/watched names).
* ``full``   - the "Full refresh" button (scan + shortlist research).

Each has its own on/off, interval and market-hours switch, stored in
``app_settings`` rows ``autorefresh_<job>_<field>`` (edited on the Settings
page, no restart): saving calls ``apply_schedule``, which adds, re-times or
removes the ``autorefresh-<job>`` jobs on the scheduler the web process armed
at startup.  The nightly cron is separate.

The two never run at the same time (nor alongside the nightly run or a
button press): every tick goes through ``runner.run_when_free``, which waits
and re-checks every ``POLL_SECONDS`` until the running job finishes.
APScheduler's ``max_instances=1`` + ``coalesce`` mean a timer that is already
waiting doesn't queue more copies of itself, so a short interval can't pile up.
A market-hours-only tick that waited past the close is dropped.  "Market
hours" = Mon-Fri 09:30-16:15 New York time (the extra 15 minutes catch the
closing print); like ``app/trading/freshness`` there is deliberately no
holiday calendar.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy.orm import Session

from app.background import runner
from app.db.models import AppSetting

logger = logging.getLogger("stock-advisor")

MODES = ("prices", "full")
JOB_IDS = {mode: f"autorefresh-{mode}" for mode in MODES}
JOB_NAMES = {"prices": "auto-refresh prices", "full": "auto-refresh full"}
LEGACY_JOB_ID = "autorefresh"

MIN_MINUTES = 5
MAX_MINUTES = 24 * 60
#: A full refresh takes an hour or two, so its timer starts at an hour.
MIN_MINUTES_BY_MODE = {"prices": MIN_MINUTES, "full": 60}
DEFAULT_MINUTES = {"prices": 60, "full": 720}
PRESETS = {"prices": (5, 10, 15, 30, 60, 120, 240, 720),
           "full": (60, 120, 240, 360, 720, 1440)}

#: How often a waiting tick re-checks whether the other job has finished,
#: and the longest it waits before giving its turn up.
POLL_SECONDS = 30.0
MAX_WAIT_SECONDS = 6 * 3600

NEW_YORK = ZoneInfo("America/New_York")
MARKET_OPEN = dt.time(9, 30)
MARKET_CLOSE = dt.time(16, 15)

FIELDS = ("enabled", "minutes", "market_hours_only")
#: Single-timer config from before the split: one job, picked by ``mode``.
LEGACY_KEYS = {"enabled": "autorefresh_enabled", "mode": "autorefresh_mode",
               "minutes": "autorefresh_minutes",
               "market_hours_only": "autorefresh_market_hours_only"}

#: The web process's scheduler, set in ``app.main`` at startup (``None`` in
#: tests, CLI and one-shot containers - saving the config then only persists).
_scheduler = None


def key(mode: str, name: str) -> str:
    return f"autorefresh_{mode}_{name}"


@dataclass(frozen=True)
class Schedule:
    enabled: bool = False
    minutes: int = 60
    market_hours_only: bool = True


@dataclass(frozen=True)
class AutoRefreshConfig:
    prices: Schedule = field(default_factory=lambda: Schedule(False, DEFAULT_MINUTES["prices"], True))
    full: Schedule = field(default_factory=lambda: Schedule(False, DEFAULT_MINUTES["full"], False))

    def for_mode(self, mode: str) -> Schedule:
        return getattr(self, mode)


def parse_minutes(raw: str | None, default: int = DEFAULT_MINUTES["prices"],
                  minimum: int = MIN_MINUTES) -> int:
    """Clamp to [minimum, MAX_MINUTES]; garbage falls back to ``default``."""
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return max(minimum, min(MAX_MINUTES, value))


def parse_mode(raw: str | None) -> str:
    return raw if raw in MODES else "prices"


def load_config(session: Session) -> AutoRefreshConfig:
    wanted = [key(m, f) for m in MODES for f in FIELDS] + list(LEGACY_KEYS.values())
    rows = {s.key: s.value for s in session.query(AppSetting)
            .filter(AppSetting.key.in_(wanted))}
    defaults = AutoRefreshConfig()
    legacy_mode = parse_mode(rows.get(LEGACY_KEYS["mode"]))
    schedules = {}
    for mode in MODES:
        default = defaults.for_mode(mode)
        if key(mode, "enabled") not in rows and LEGACY_KEYS["enabled"] in rows:
            # Saved before the split: the old timer becomes this job's timer
            # if it ran this job; the other job starts off.
            if mode != legacy_mode:
                schedules[mode] = default
                continue
            get = {f: rows.get(LEGACY_KEYS[f]) for f in FIELDS}
        else:
            get = {f: rows.get(key(mode, f)) for f in FIELDS}
        schedules[mode] = Schedule(
            enabled=get["enabled"] == "1",
            minutes=parse_minutes(get["minutes"], default.minutes, MIN_MINUTES_BY_MODE[mode]),
            market_hours_only=(get["market_hours_only"] == "1"
                               if get["market_hours_only"] is not None
                               else default.market_hours_only),
        )
    return AutoRefreshConfig(**schedules)


def save_config(session: Session, config: AutoRefreshConfig) -> None:
    """Upsert every field of both timers and drop the pre-split rows (caller commits)."""
    now = dt.datetime.now(dt.timezone.utc)
    for mode in MODES:
        schedule = config.for_mode(mode)
        values = {
            "enabled": "1" if schedule.enabled else "0",
            "minutes": str(parse_minutes(str(schedule.minutes), DEFAULT_MINUTES[mode],
                                         MIN_MINUTES_BY_MODE[mode])),
            "market_hours_only": "1" if schedule.market_hours_only else "0",
        }
        for name, value in values.items():
            row = session.query(AppSetting).filter_by(key=key(mode, name)).first()
            if row is None:
                row = AppSetting(key=key(mode, name), value=value)
                session.add(row)
            row.value = value
            row.updated_at = now
    session.query(AppSetting).filter(
        AppSetting.key.in_(LEGACY_KEYS.values())).delete(synchronize_session=False)
    session.flush()


def market_open(now: dt.datetime | None = None) -> bool:
    """Mon-Fri 09:30-16:15 New York time (no holiday calendar on purpose)."""
    local = (now or dt.datetime.now(dt.timezone.utc)).astimezone(NEW_YORK)
    return local.weekday() < 5 and MARKET_OPEN <= local.time() <= MARKET_CLOSE


def job_for(mode: str):
    from app.jobs import run_pipeline, run_price_refresh  # heavy research imports
    return run_pipeline if mode == "full" else run_price_refresh


def run_tick(market_hours_only: bool, mode: str = "prices", *,
             now: dt.datetime | None = None, job=None,
             poll_seconds: float = POLL_SECONDS, max_wait_seconds: float = MAX_WAIT_SECONDS,
             sleep=None) -> str:
    """One scheduled tick: ``'ran'``, ``'closed'`` or ``'timeout'``.

    If another job is running, waits (re-checking every ``poll_seconds``)
    and then runs; a market-hours tick that waits past the close is dropped.
    """
    def wanted() -> bool:
        return not market_hours_only or market_open(now)

    if not wanted():
        return "closed"
    if job is None:
        job = job_for(mode)
    kwargs = {"sleep": sleep} if sleep is not None else {}
    outcome = runner.run_when_free(JOB_NAMES[mode], job, poll_seconds=poll_seconds,
                                   max_wait_seconds=max_wait_seconds,
                                   still_wanted=wanted, **kwargs)
    if outcome == "cancelled":
        logger.info("auto-refresh %s dropped: the market closed while it waited", mode)
        return "closed"
    return outcome


def set_scheduler(scheduler) -> None:
    global _scheduler
    _scheduler = scheduler


def apply_schedule(config: AutoRefreshConfig, scheduler=None) -> None:
    """Add, re-time or remove both interval jobs to match ``config``."""
    scheduler = scheduler if scheduler is not None else _scheduler
    if scheduler is None:
        return
    if scheduler.get_job(LEGACY_JOB_ID) is not None:
        scheduler.remove_job(LEGACY_JOB_ID)
    for mode in MODES:
        schedule, job_id = config.for_mode(mode), JOB_IDS[mode]
        if not schedule.enabled:
            if scheduler.get_job(job_id) is not None:
                scheduler.remove_job(job_id)
            logger.info("auto-refresh %s off", mode)
            continue
        scheduler.add_job(run_tick, IntervalTrigger(minutes=schedule.minutes),
                          kwargs={"market_hours_only": schedule.market_hours_only,
                                  "mode": mode},
                          id=job_id, replace_existing=True,
                          max_instances=1, coalesce=True, misfire_grace_time=None)
        logger.info("auto-refresh %s every %d min%s", mode, schedule.minutes,
                    " (market hours only)" if schedule.market_hours_only else "")


def next_run_at(mode: str) -> dt.datetime | None:
    """When that timer fires next (``None`` if off or no scheduler)."""
    if _scheduler is None:
        return None
    job = _scheduler.get_job(JOB_IDS[mode])
    return job.next_run_time if job is not None else None


def scheduler_running() -> bool:
    return _scheduler is not None
