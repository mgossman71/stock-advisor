"""The nightly AI time budget: priority order, cut-off, and reuse.

A fake clock advances 40 s per model call, so a 1-minute budget buys exactly
two calls - no test sleeps, no network.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal as D

from app.db.models import AnalystAction, PriceHistory, ResearchReport
from app.ingestion.ingest import ensure_ticker
from app.research.llm import LLMConfig
from app.research.llm_advisor import LLMAdvisor
from app.research.scoring import ensure_default_weights
from app.research.screener import (
    AIBudget, reusable_ai_report, screen_universe, screening_candidates,
)
from tests.test_llm_advisor import FakeClient, _analysis

TODAY = dt.date.today()


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class TimedClient(FakeClient):
    """A model that takes 40 s (of fake time) per answer."""

    def __init__(self, clock: Clock):
        super().__init__(_analysis(recommendation={"action": "none"}))
        self.clock = clock

    def chat(self, messages, *, json_mode=True):
        self.clock.now += 40
        return super().chat(messages, json_mode=json_mode)


def _watched(session, symbol: str, close: str = "100"):
    t = ensure_ticker(session, symbol, in_watchlist=True, asset_class="stock")
    for i in range(80):
        session.add(PriceHistory(ticker_id=t.id, date=TODAY - dt.timedelta(days=79 - i),
                                 close=D(close) + i % 3, adj_close=D(close) + i % 3,
                                 source="fake"))
    session.flush()
    return t


def _setup(session, minutes: int = 1):
    ensure_default_weights(session)
    for symbol in ("AAA", "BBB", "CCC"):
        _watched(session, symbol)
    clock = Clock()
    client = TimedClient(clock)
    config = LLMConfig(enabled=True, base_url="http://llm/v1", model="m",
                       ai_minutes_per_night=minutes)
    return LLMAdvisor(config, client=client), client, clock


def _latest(session, symbol: str) -> ResearchReport:
    t = ensure_ticker(session, symbol)
    return session.query(ResearchReport).filter_by(ticker_id=t.id) \
        .order_by(ResearchReport.id.desc()).first()


def test_budget_runs_out_in_priority_order(session):
    advisor, client, clock = _setup(session, minutes=1)
    stats = screen_universe(session, advisor=advisor, clock=clock)
    assert (stats.ai_used, stats.ai_over_budget, stats.ai_reused) == (2, 1, 0)
    assert len(client.calls) == 2
    assert _latest(session, "AAA").model_or_rule_version == "llm:m"
    assert _latest(session, "BBB").model_or_rule_version == "llm:m"
    ccc = _latest(session, "CCC")
    assert ccc.model_or_rule_version == "rules-v1"
    assert ccc.payload_json["ai_skipped"] == "budget"
    assert "AI analyst: 2 read, 0 reused" in stats.notes[-1]


def test_unchanged_names_reuse_their_report_and_free_the_budget(session):
    advisor, client, clock = _setup(session, minutes=1)
    screen_universe(session, advisor=advisor, clock=clock)
    # Second night, fresh budget: AAA/BBB are unchanged -> reused, so the
    # budget goes to CCC, which missed out last night.
    clock2 = Clock()
    advisor2 = LLMAdvisor(advisor.config, client=TimedClient(clock2))
    stats = screen_universe(session, advisor=advisor2, clock=clock2)
    assert (stats.ai_reused, stats.ai_used, stats.ai_over_budget) == (2, 1, 0)
    assert _latest(session, "CCC").model_or_rule_version == "llm:m"


def test_reuse_is_refused_when_something_material_changed(session):
    advisor, _, clock = _setup(session, minutes=5)
    screen_universe(session, advisor=advisor, clock=clock)
    aaa = ensure_ticker(session, "AAA")
    assert reusable_ai_report(session, aaa, TODAY) is not None
    # Too old.
    report = _latest(session, "AAA")
    report.created_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=10)
    session.flush()
    assert reusable_ai_report(session, aaa, TODAY) is None
    # Fresh again, but an analyst acted after it.
    report.created_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=2)
    session.add(AnalystAction(ticker_id=aaa.id, action_date=TODAY, action="upgrade",
                              source="fake"))
    session.flush()
    assert reusable_ai_report(session, aaa, TODAY) is None
    # The lens score moved a lot.
    bbb = ensure_ticker(session, "BBB")
    _latest(session, "BBB").payload_json = {**_latest(session, "BBB").payload_json,
                                            "lens_score": "40"}
    session.flush()
    assert reusable_ai_report(session, bbb, TODAY, lens_score=D("45")) is not None
    assert reusable_ai_report(session, bbb, TODAY, lens_score=D("60")) is None


def test_price_move_over_five_percent_forces_a_fresh_read(session):
    advisor, _, clock = _setup(session, minutes=5)
    screen_universe(session, advisor=advisor, clock=clock)
    aaa = ensure_ticker(session, "AAA")
    report = _latest(session, "AAA")
    report.created_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=2)
    session.add(PriceHistory(ticker_id=aaa.id, date=TODAY + dt.timedelta(days=1),
                             close=D("130"), source="fake"))
    session.flush()
    assert reusable_ai_report(session, aaa, TODAY) is None


def test_priority_order_is_owned_then_emerging_then_shortlist(session, monkeypatch):
    for symbol in ("ZED", "ACE"):
        ensure_ticker(session, symbol, in_watchlist=True)
    for symbol in ("EM1", "EM2", "SL1"):
        ensure_ticker(session, symbol)
    session.flush()
    monkeypatch.setattr("app.research.scan.emerging_priority", lambda s: ["EM2", "EM1"])
    monkeypatch.setattr("app.research.scan.shortlist_symbols", lambda s: ["SL1", "EM1", "ACE"])
    assert [t.symbol for t in screening_candidates(session)] == \
        ["ACE", "ZED", "EM2", "EM1", "SL1"]


def test_budget_object():
    clock = Clock()
    budget = AIBudget(2, clock)
    assert budget.left()
    clock.now = 119
    assert budget.left()
    clock.now = 120
    assert not budget.left()
