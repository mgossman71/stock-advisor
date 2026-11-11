"""Interval auto-refresh: config round-trip, market-hours gate, live re-arming."""

from __future__ import annotations

import datetime as dt

import pytest
from apscheduler.schedulers.background import BackgroundScheduler

from app import autorefresh
from app.autorefresh import AutoRefreshConfig, apply_schedule, load_config, run_tick
from app.background import runner
from app.db.base import session_scope
from tests.test_web_pages import client  # noqa: F401 - shared fixture

UTC = dt.timezone.utc


def test_defaults_are_off_hourly_market_hours(session):
    assert load_config(session) == AutoRefreshConfig(False, 60, True)


@pytest.mark.parametrize("raw,expected", [
    ("30", 30), ("1", autorefresh.MIN_MINUTES), ("99999", autorefresh.MAX_MINUTES),
    ("abc", 60), ("", 60), (None, 60),
])
def test_minutes_are_clamped(raw, expected):
    assert autorefresh.parse_minutes(raw) == expected


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


def test_tick_skips_when_closed_and_when_busy():
    calls = []
    saturday = dt.datetime(2026, 11, 14, 16, 0, tzinfo=UTC)
    assert run_tick(True, now=saturday, job=calls.append) == "closed"
    assert calls == []
    assert run_tick(False, now=saturday, job=lambda progress: calls.append(1)) == "ran"
    assert calls == [1]
    assert runner.status().name == "auto-refresh"

    assert runner._claim("refresh")  # simulate a manual run in flight
    try:
        assert run_tick(False, job=lambda progress: calls.append(2)) == "busy"
    finally:
        runner._status.running = False
    assert calls == [1]


def test_apply_schedule_adds_retimes_and_removes():
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.start(paused=True)
    try:
        apply_schedule(AutoRefreshConfig(True, 15, True), scheduler)
        job = scheduler.get_job(autorefresh.JOB_ID)
        assert job.trigger.interval == dt.timedelta(minutes=15)
        assert job.kwargs == {"market_hours_only": True}

        apply_schedule(AutoRefreshConfig(True, 120, False), scheduler)
        job = scheduler.get_job(autorefresh.JOB_ID)
        assert job.trigger.interval == dt.timedelta(minutes=120)
        assert job.kwargs == {"market_hours_only": False}
        assert len(scheduler.get_jobs()) == 1

        apply_schedule(AutoRefreshConfig(False, 120, False), scheduler)
        assert scheduler.get_job(autorefresh.JOB_ID) is None
        apply_schedule(AutoRefreshConfig(False, 120, False), scheduler)  # idempotent
    finally:
        scheduler.shutdown(wait=False)


def test_settings_form_saves_and_rearms_the_live_scheduler(client, monkeypatch):
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.start(paused=True)
    monkeypatch.setattr(autorefresh, "_scheduler", scheduler)
    try:
        html = client.get("/settings").text
        assert 'id="autorefresh"' in html and "Auto-refresh" in html

        r = client.post("/settings/autorefresh", follow_redirects=False, data={
            "autorefresh_enabled": "1", "autorefresh_minutes": "30",
            "autorefresh_market_hours_only": "1"})
        assert r.status_code == 303
        with session_scope() as s:
            assert load_config(s) == AutoRefreshConfig(True, 30, True)
        job = scheduler.get_job(autorefresh.JOB_ID)
        assert job.trigger.interval == dt.timedelta(minutes=30)
        assert "on · every 30 min" in client.get("/settings").text

        client.post("/settings/autorefresh", data={"autorefresh_minutes": "30"})
        with session_scope() as s:
            assert load_config(s) == AutoRefreshConfig(False, 30, False)
        assert scheduler.get_job(autorefresh.JOB_ID) is None
    finally:
        scheduler.shutdown(wait=False)
