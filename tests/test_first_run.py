"""A fresh install sets itself up; long jobs run in the background with progress.

Pins: the single-flight runner, first-run detection, the pipeline loading
the universe when it is empty (no network - ``load_universe`` is faked),
startup kicking setup off only for a fresh DB in the web process, and the
refresh / screen buttons returning a polling progress panel immediately.
"""

from __future__ import annotations

import datetime as dt
import threading
from decimal import Decimal as D

import pytest

from app import background
from app.background import JobRunner
from app.config import Settings, get_settings
from app.db.models import PriceHistory, ResearchReport, Ticker
from app.ingestion.ingest import ensure_ticker
from app.ingestion.universe import UniverseResult
from app.jobs import needs_first_run, run_nightly
from app.providers.base import UniverseMember
from app.research.scoring import ensure_default_weights
from tests.conftest import FakeProvider, make_bar
from tests.test_web_pages import client  # noqa: F401 - shared fixture

DAY = dt.date(2026, 1, 5)


# ------------------------------------------------------------------- runner

def test_runner_is_single_flight_and_reports_progress():
    runner = JobRunner()
    release = threading.Event()
    seen = threading.Event()

    def job(progress):
        progress("fetching prices", 3, 10, "AAPL")
        seen.set()
        release.wait(5)
        return "ok"

    assert runner.start("refresh", job)
    assert seen.wait(5)
    status = runner.status()
    assert status.running and status.name == "refresh"
    assert (status.phase, status.done, status.total, status.detail) == \
        ("fetching prices", 3, 10, "AAPL")
    assert not runner.start("screen", job), "second job must not start"
    assert not runner.run_blocking("nightly", job)
    release.set()
    for _ in range(500):
        if not runner.status().running:
            break
        threading.Event().wait(0.01)
    done = runner.status()
    assert not done.running and done.result == "ok" and done.phase == "done"


def test_runner_captures_failures():
    runner = JobRunner()

    def boom(progress):
        raise RuntimeError("provider down")

    assert runner.run_blocking("refresh", boom)
    status = runner.status()
    assert status.error == "provider down" and status.phase == "failed"
    assert runner.run_blocking("refresh", lambda p: 1)  # free again


# ---------------------------------------------------------------- pipeline

def test_needs_first_run(session):
    assert needs_first_run(session)  # empty DB
    t = ensure_ticker(session, "ACME", in_universe=True)
    assert needs_first_run(session)  # universe but never screened
    session.add(ResearchReport(ticker_id=t.id, model_or_rule_version="rules-v1",
                               summary="s", overall_stance="neutral", score=D("50"),
                               payload_json={}))
    session.flush()
    assert not needs_first_run(session)
    # A seed-only DB: big universe, a handful researched -> still set up.
    for i in range(4):
        ensure_ticker(session, f"U{i}", in_universe=True)
    session.flush()
    assert needs_first_run(session)


def test_pipeline_loads_universe_then_fetches_and_screens(session, monkeypatch):
    ensure_default_weights(session)
    settings = Settings(benchmark_symbol="^SPX", vix_symbol="^VIX", slippage_bps=D("0"))
    monkeypatch.setattr("app.jobs.load_universe", lambda s: UniverseResult(
        source="fake", members=[UniverseMember("ACME", "Acme", "Industrials")], notes=[]))
    bars = [make_bar(sym, DAY - dt.timedelta(days=90 - i), str(100 + i))
            for sym in ("ACME", "^SPX", "^VIX") for i in range(90)]
    phases: list[str] = []

    report = run_nightly(session, FakeProvider(bars=bars), settings=settings, today=DAY,
                         load_universe_if_empty=True,
                         progress=lambda phase, done, total, detail: phases.append(phase))

    acme = session.query(Ticker).filter_by(symbol="ACME").one()
    assert acme.in_universe and acme.sector == "Industrials"
    assert "universe loaded: 1 tickers from fake" in report.notes
    assert set(report.fetched) == {"ACME", "^SPX", "^VIX"}
    assert session.query(PriceHistory).filter_by(ticker_id=acme.id).count() >= 60
    assert report.scored == 1
    assert not needs_first_run(session)
    assert phases[0] == "loading the stock list"
    assert {"fetching prices", "screening", "grading past calls"} <= set(phases)


def test_pipeline_leaves_an_existing_universe_alone(session, monkeypatch):
    ensure_ticker(session, "ACME", in_universe=True)
    monkeypatch.setattr("app.jobs.load_universe",
                        lambda s: pytest.fail("universe must not reload"))
    run_nightly(session, FakeProvider(), settings=Settings(), today=DAY,
                load_universe_if_empty=True)


# ----------------------------------------------------------------- startup

@pytest.fixture
def startup(tmp_path, monkeypatch):
    """Call the real startup hook against a throwaway DB, scheduler stubbed."""
    from app.db.base import Base, dispose_all, get_engine

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'boot.db'}")
    monkeypatch.setenv("SCHEDULER_ENABLED", "1")
    get_settings.cache_clear()
    dispose_all()
    Base.metadata.create_all(get_engine())

    class NoScheduler:
        def __init__(self, **kw): ...
        def add_job(self, *a, **kw): ...
        def start(self): ...
        def shutdown(self, wait=False): ...

    started: list[str] = []
    import app.main as main

    monkeypatch.setattr(main, "BackgroundScheduler", NoScheduler)
    monkeypatch.setattr(main.runner, "start", lambda name, job: started.append(name) or True)
    yield main, started
    dispose_all()
    get_settings.cache_clear()


def test_startup_sets_up_a_fresh_install(startup):
    main, started = startup
    main.start_scheduler()
    assert started == ["first-run setup"]


def test_startup_skips_setup_when_already_screened_or_disabled(startup, monkeypatch):
    main, started = startup
    from app.db.base import session_scope

    with session_scope() as s:
        t = ensure_ticker(s, "ACME", in_universe=True)
        s.add(ResearchReport(ticker_id=t.id, model_or_rule_version="rules-v1", summary="s",
                             overall_stance="neutral", score=D("50"), payload_json={}))
        s.commit()
    main.start_scheduler()
    assert started == []

    monkeypatch.setenv("AUTO_SETUP", "0")
    get_settings.cache_clear()
    with session_scope() as s:
        s.query(ResearchReport).delete()
        s.commit()
    main.start_scheduler()
    assert started == []


# --------------------------------------------------------------------- web

def test_refresh_button_returns_a_polling_progress_panel(client, monkeypatch):
    runner = JobRunner()
    monkeypatch.setattr("app.web.routes_jobs.runner", runner)
    release = threading.Event()

    def slow_pipeline(progress, **kw):
        progress("fetching prices", 4, 100, "MSFT")
        release.wait(5)

    monkeypatch.setattr("app.web.routes_jobs.run_pipeline", slow_pipeline)
    started = client.post("/jobs/refresh").text
    assert 'hx-trigger="every 3s"' in started  # returned at once, polling
    for _ in range(500):  # wait until the job has reported progress
        if runner.status().done == 4:
            break
        threading.Event().wait(0.01)
    html = client.get("/jobs/status?watching=1").text
    assert 'hx-trigger="every 3s"' in html
    assert "fetching prices MSFT" in html and "4 / 100" in html
    release.set()


def test_finished_job_shows_its_result_only_to_the_watcher(client, monkeypatch):
    from app.jobs import JobReport

    runner = JobRunner()
    runner.run_blocking("refresh", lambda p: JobReport(fetched=["A", "B"], scored=2,
                                                       new_recommendations=1))
    monkeypatch.setattr("app.web.routes_jobs.runner", runner)
    watched = client.get("/jobs/status?watching=1").text
    assert "Refresh job done." in watched and "new calls=1" in watched
    assert "every 3s" not in watched  # polling stops
    assert client.get("/jobs/status").text.strip() == ""  # plain page loads: quiet


def test_screen_now_runs_in_the_background(client, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr("app.web.routes_screen.runner.start",
                        lambda name, job: calls.append(name) or True)
    resp = client.post("/screen/run")
    assert resp.status_code == 200 and calls == ["screen"]


def test_every_page_asks_for_job_status_on_load(client):
    assert 'hx-get="/jobs/status" hx-trigger="load"' in client.get("/").text


def test_module_runner_is_shared():
    from app.web import routes_jobs, routes_screen
    import app.main as main

    assert routes_jobs.runner is routes_screen.runner is main.runner is background.runner
