"""Interval auto-refresh: config round-trip, market-hours gate, live re-arming."""

from __future__ import annotations

import datetime as dt

import pytest
from apscheduler.schedulers.background import BackgroundScheduler

from app import autorefresh
from app.autorefresh import (
    AutoRefreshConfig, Schedule, apply_schedule, load_config, run_tick, save_config,
)
from app.background import runner
from app.db.base import session_scope
from app.db.models import AppSetting
from tests.test_web_pages import client  # noqa: F401 - shared fixture

UTC = dt.timezone.utc


def test_defaults_are_both_off_and_independent(session):
    config = load_config(session)
    assert config.prices == Schedule(False, 60, True)
    assert config.full == Schedule(False, 720, False)


@pytest.mark.parametrize("raw,expected", [
    ("30", 30), ("1", autorefresh.MIN_MINUTES), ("99999", autorefresh.MAX_MINUTES),
    ("abc", 60), ("", 60), (None, 60),
])
def test_minutes_are_clamped(raw, expected):
    assert autorefresh.parse_minutes(raw) == expected


def test_both_timers_round_trip_and_full_has_an_hour_floor(session):
    save_config(session, AutoRefreshConfig(Schedule(True, 15, True), Schedule(True, 10, False)))
    config = load_config(session)
    assert config.prices == Schedule(True, 15, True)
    assert config.full == Schedule(True, 60, False), "full refresh can't run more than hourly"


def test_pre_split_config_carries_over_to_its_job(session):
    for k, v in (("autorefresh_enabled", "1"), ("autorefresh_mode", "full"),
                 ("autorefresh_minutes", "240"), ("autorefresh_market_hours_only", "0")):
        session.add(AppSetting(key=k, value=v))
    session.flush()
    config = load_config(session)
    assert config.full == Schedule(True, 240, False)
    assert config.prices == AutoRefreshConfig().prices
    save_config(session, config)
    assert session.query(AppSetting).filter(AppSetting.key == "autorefresh_mode").count() == 0
    assert load_config(session) == config


@pytest.mark.parametrize("when,is_open", [
    (dt.datetime(2026, 11, 11, 14, 29, tzinfo=UTC), False),  # Wed 09:29 EST
    (dt.datetime(2026, 11, 11, 14, 30, tzinfo=UTC), True),   # Wed 09:30 EST
    (dt.datetime(2026, 11, 11, 21, 15, tzinfo=UTC), True),   # Wed 16:15 EST
    (dt.datetime(2026, 11, 11, 21, 16, tzinfo=UTC), False),
    (dt.datetime(2026, 7, 15, 13, 30, tzinfo=UTC), True),    # Wed 09:30 EDT
    (dt.datetime(2026, 11, 14, 16, 0, tzinfo=UTC), False),   # Saturday
])
def test_market_hours_follow_new_york_time(when, is_open):
    assert autorefresh.market_open(when) is is_open


def test_tick_skips_when_closed_and_runs_when_free():
    calls = []
    saturday = dt.datetime(2026, 11, 14, 16, 0, tzinfo=UTC)
    assert run_tick(True, now=saturday, job=calls.append) == "closed"
    assert calls == []
    assert run_tick(False, now=saturday, job=lambda progress: calls.append(1)) == "ran"
    assert calls == [1]
    assert runner.status().name == "auto-refresh prices"


def test_busy_tick_waits_for_the_other_job_then_runs():
    """The other job finishes after two polls; the tick then runs - never alongside it."""
    calls, polls = [], []
    assert runner._claim("auto-refresh full")  # a full refresh in flight

    def fake_sleep(seconds):
        polls.append(seconds)
        assert "auto-refresh prices" in runner.waiting()
        assert calls == [], "must not run while the other job holds the runner"
        if len(polls) == 2:
            runner._status.running = False  # the full refresh finishes

    try:
        outcome = run_tick(False, "prices", job=lambda p: calls.append(runner.status().name),
                           poll_seconds=30, sleep=fake_sleep)
    finally:
        runner._status.running = False
    assert outcome == "ran" and polls == [30, 30]
    assert calls == ["auto-refresh prices"]
    assert runner.waiting() == {}


def test_waiting_tick_gives_up_and_drops_after_the_close():
    calls = []
    assert runner._claim("nightly")
    try:
        assert run_tick(False, "full", job=calls.append, poll_seconds=60,
                        max_wait_seconds=120, sleep=lambda s: None) == "timeout"

        times = iter([dt.datetime(2026, 11, 11, 21, 0, tzinfo=UTC),    # 16:00 NY, open
                      dt.datetime(2026, 11, 11, 21, 30, tzinfo=UTC)])  # 16:30, closed
        real = autorefresh.market_open
        autorefresh.market_open = lambda now=None: real(next(times))
        try:
            assert run_tick(True, "prices", job=calls.append, sleep=lambda s: None) == "closed"
        finally:
            autorefresh.market_open = real
    finally:
        runner._status.running = False
    assert calls == []
    assert runner.waiting() == {}


def test_apply_schedule_runs_two_independent_timers():
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.start(paused=True)
    try:
        scheduler.add_job(lambda: None, "interval", minutes=5, id=autorefresh.LEGACY_JOB_ID)
        apply_schedule(AutoRefreshConfig(Schedule(True, 15, True), Schedule(True, 240, False)),
                       scheduler)
        prices = scheduler.get_job(autorefresh.JOB_IDS["prices"])
        full = scheduler.get_job(autorefresh.JOB_IDS["full"])
        assert prices.trigger.interval == dt.timedelta(minutes=15)
        assert prices.kwargs == {"market_hours_only": True, "mode": "prices"}
        assert full.trigger.interval == dt.timedelta(minutes=240)
        assert full.kwargs == {"market_hours_only": False, "mode": "full"}
        assert scheduler.get_job(autorefresh.LEGACY_JOB_ID) is None
        assert len(scheduler.get_jobs()) == 2

        apply_schedule(AutoRefreshConfig(Schedule(False, 15, True), Schedule(True, 120, False)),
                       scheduler)
        assert scheduler.get_job(autorefresh.JOB_IDS["prices"]) is None
        assert scheduler.get_job(autorefresh.JOB_IDS["full"]).trigger.interval == \
            dt.timedelta(minutes=120)

        apply_schedule(AutoRefreshConfig(), scheduler)
        apply_schedule(AutoRefreshConfig(), scheduler)  # idempotent
        assert scheduler.get_jobs() == []
    finally:
        scheduler.shutdown(wait=False)


def test_settings_form_saves_and_rearms_the_live_scheduler(client, monkeypatch):
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.start(paused=True)
    monkeypatch.setattr(autorefresh, "_scheduler", scheduler)
    try:
        html = client.get("/settings").text
        assert 'id="autorefresh"' in html and 'name="full_minutes"' in html

        r = client.post("/settings/autorefresh", follow_redirects=False, data={
            "prices_enabled": "1", "prices_minutes": "15", "prices_market_hours_only": "1",
            "full_enabled": "1", "full_minutes": "240"})
        assert r.status_code == 303
        with session_scope() as s:
            assert load_config(s) == AutoRefreshConfig(Schedule(True, 15, True),
                                                       Schedule(True, 240, False))
        assert scheduler.get_job(autorefresh.JOB_IDS["prices"]).trigger.interval == \
            dt.timedelta(minutes=15)
        assert scheduler.get_job(autorefresh.JOB_IDS["full"]).trigger.interval == \
            dt.timedelta(minutes=240)

        client.post("/settings/autorefresh", data={"prices_minutes": "30",
                                                   "full_enabled": "1", "full_minutes": "bogus"})
        with session_scope() as s:
            config = load_config(s)
        assert config.prices == Schedule(False, 30, False)
        assert config.full == Schedule(True, 240, False), "garbage keeps the current interval"
        assert scheduler.get_job(autorefresh.JOB_IDS["prices"]) is None
        assert scheduler.get_job(autorefresh.JOB_IDS["full"]) is not None
    finally:
        scheduler.shutdown(wait=False)


def test_mode_picks_the_job():
    from app.jobs import run_pipeline, run_price_refresh
    assert autorefresh.job_for("prices") is run_price_refresh
    assert autorefresh.job_for("full") is run_pipeline
    assert autorefresh.parse_mode("nonsense") == "prices"


def test_settings_shows_what_is_running_and_what_is_waiting(client):
    assert runner._claim("auto-refresh full")
    runner._waiting["auto-refresh prices"] = dt.datetime(2026, 11, 11, 15, 0, tzinfo=UTC)
    try:
        html = client.get("/settings").text
    finally:
        runner._waiting.clear()
        runner._status.running = False
    assert "Running now: <b>the full auto-refresh</b>" in html
    assert "Waiting for it to finish: <b>the prices auto-refresh</b>" in html
