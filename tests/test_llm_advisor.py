"""LLM advisor: what the model may decide, what it may never decide, and fallbacks.

No network: a scripted fake client stands in for the model, and the HTTP
client is exercised through ``httpx.MockTransport``.
"""

from __future__ import annotations

import json
from decimal import Decimal as D

import httpx
import pytest

from app.db.models import Recommendation
from app.research.engine import RuleBasedAdvisor
from app.research.llm import (
    LLMClient, LLMConfig, LLMError, LLMUnreachable, config_from_form, extract_json,
    load_llm_config, save_llm_config,
)
from app.research.llm_advisor import LLMAdvisor, advisor_for, validate_analysis
from app.research.scoring import ensure_default_weights
from tests.test_advisor import _latest, _seed_bars, _seed_strong_fundamentals

CONFIG = LLMConfig(enabled=True, base_url="http://llm.local/v1", model="test-model")


class FakeClient:
    """Plays the model: returns scripted replies (or raises) in order."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls: list[list[dict]] = []

    def chat(self, messages, *, json_mode=True):
        self.calls.append(messages)
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, Exception):
            raise reply
        return reply if isinstance(reply, str) else json.dumps(reply)


def _analysis(**overrides) -> dict:
    call = {"action": "long", "confidence": 72, "horizon": "medium",
            "target_price": None, "stop_price": None, "rationale": "cheap and rising"}
    call.update(overrides.pop("recommendation", {}))
    base = {"stance": "bullish", "score": 74, "summary": "Strong trend, fair value.",
            "bull_points": ["ROE 25%"], "bear_points": [], "risks": ["beta 0.9"],
            "recommendation": call}
    base.update(overrides)
    return base


@pytest.fixture
def winner(session):
    ensure_default_weights(session)
    _seed_bars(session, "WINNER", 300, D("100"), D("0.25"))
    _seed_strong_fundamentals(session, "WINNER")
    return _latest(session, "WINNER")


def test_model_view_becomes_the_report_and_rules_view_is_kept(session, winner):
    client = FakeClient(_analysis())
    report = LLMAdvisor(CONFIG, client=client).research(session, "WINNER")
    assert report.model_or_rule_version == "llm:test-model"
    assert report.overall_stance == "bullish"
    assert report.score == D("74.00")
    assert report.summary == "Strong trend, fair value."
    assert report.payload_json["bull_points"] == ["ROE 25%"]
    rules = report.payload_json["rules"]
    assert rules["stance"] in ("bullish", "neutral", "bearish") and rules["score"]
    assert report.payload_json["evidence"], "cited rule evidence survives"


def test_dossier_carries_stored_data_and_nulls_stay_null(session, winner):
    client = FakeClient(_analysis())
    LLMAdvisor(CONFIG, client=client).research(session, "WINNER")
    dossier = json.loads(client.calls[0][1]["content"])
    assert dossier["ticker"]["symbol"] == "WINNER"
    assert D(dossier["last_close"]["close"]) == winner
    assert dossier["fundamentals"]["return_on_equity_pct"] == "25.0000"
    assert dossier["fundamentals"]["market_cap"] is None  # unknown, not 0
    assert dossier["rules_baseline"]["sub_scores"]
    assert len(dossier["analyst_actions_90d"]) == 2


def test_entry_is_the_stored_close_never_the_models_price(session, winner):
    reply = _analysis(recommendation={"entry_price": 1, "target_price": float(winner) * 1.3,
                                      "stop_price": float(winner) * 0.9})
    advisor = LLMAdvisor(CONFIG, client=FakeClient(reply))
    report = advisor.research(session, "WINNER")
    [rec] = advisor.recommendations_for(session, report)
    assert rec.direction == "long"
    assert rec.entry_price_at_call == winner
    assert rec.target_price == D(str(float(winner) * 1.3))
    assert rec.engine_version == "llm:test-model"
    assert rec.confidence == D("72.0")


def test_wrong_side_levels_fall_back_to_defaults_and_say_so(session, winner):
    reply = _analysis(recommendation={"target_price": float(winner) * 0.5,  # below entry
                                      "stop_price": float(winner) * 1.1})   # above entry
    advisor = LLMAdvisor(CONFIG, client=FakeClient(reply))
    [rec] = advisor.recommendations_for(session, advisor.research(session, "WINNER"))
    assert rec.stop_price == winner * D("0.85")
    assert rec.target_price == winner * D("1.25")
    assert "default stop + target used" in rec.rationale


@pytest.mark.parametrize("call", [
    {"action": "none"},
    {"action": "long", "confidence": 40},          # below MIN_CONFIDENCE
    {"action": "long", "confidence": None},
    {"action": "short", "confidence": 90},         # contradicts bullish stance
    {"action": "buy!!", "confidence": 90},         # not an action
])
def test_no_call_when_model_declines_or_is_incoherent(session, winner, call):
    advisor = LLMAdvisor(CONFIG, client=FakeClient(_analysis(recommendation=call)))
    report = advisor.research(session, "WINNER")
    assert advisor.recommendations_for(session, report) == []


def test_one_open_call_per_direction_still_holds(session, winner):
    advisor = LLMAdvisor(CONFIG, client=FakeClient(_analysis()))
    assert len(advisor.recommendations_for(session, advisor.research(session, "WINNER"))) == 1
    assert advisor.recommendations_for(session, advisor.research(session, "WINNER")) == []
    assert session.query(Recommendation).count() == 1


def test_bad_reply_falls_back_to_the_rules_report(session, winner):
    advisor = LLMAdvisor(CONFIG, client=FakeClient("I think it's a great stock!"))
    report = advisor.research(session, "WINNER")
    assert report.model_or_rule_version == "rules-v1"
    assert "no JSON object" in report.payload_json["llm_error"]
    assert "llm" not in report.payload_json
    # The rules engine decides instead: WINNER clears its long threshold.
    [rec] = advisor.recommendations_for(session, report)
    assert rec.direction == "long" and rec.engine_version == "rules-v1"


def test_unreachable_server_is_tried_once_per_run(session, winner):
    _seed_bars(session, "OTHER", 300, D("50"), D("0.1"))
    client = FakeClient(LLMUnreachable("connection refused"))
    advisor = LLMAdvisor(CONFIG, client=client)
    first = advisor.research(session, "WINNER")
    second = advisor.research(session, "OTHER")
    assert len(client.calls) == 1
    assert "connection refused" in first.payload_json["llm_error"]
    assert second.payload_json["llm_error"].startswith("skipped: model unreachable")


def test_validation_rejects_missing_stance_or_score():
    with pytest.raises(LLMError):
        validate_analysis(_analysis(stance="moon"))
    with pytest.raises(LLMError):
        validate_analysis(_analysis(score=140))
    with pytest.raises(LLMError):
        validate_analysis(_analysis(summary=""))
    ok = validate_analysis(_analysis(score="61.5", bull_points=["x"] * 20))
    assert ok["score"] == D("61.50") and len(ok["bull_points"]) == 8


def test_extract_json_tolerates_reasoning_and_fences():
    text = '<think>hmm {"no": 1}</think>Sure!\n```json\n{"stance": "bullish"}\n```\nDone.'
    assert extract_json(text) == {"stance": "bullish"}
    assert extract_json('prefix {"a": {"b": 2}} suffix') == {"a": {"b": 2}}
    with pytest.raises(LLMError):
        extract_json("{not json}")


# ------------------------------------------------------------- config + factory

def test_factory_picks_engine_from_settings(session):
    assert isinstance(advisor_for(session), RuleBasedAdvisor)  # nothing configured
    save_llm_config(session, CONFIG)
    assert isinstance(advisor_for(session), LLMAdvisor)
    save_llm_config(session, LLMConfig(**{**CONFIG.__dict__, "use_in_screen": False}))
    assert isinstance(advisor_for(session), LLMAdvisor)
    assert isinstance(advisor_for(session, screening=True), RuleBasedAdvisor)
    save_llm_config(session, LLMConfig(**{**CONFIG.__dict__, "model": ""}))
    assert isinstance(advisor_for(session), RuleBasedAdvisor)  # enabled but incomplete


def test_config_round_trips_and_blank_key_keeps_stored(session):
    save_llm_config(session, LLMConfig(**{**CONFIG.__dict__, "api_key": "sk-secret"}))
    stored = load_llm_config(session)
    assert stored.api_key == "sk-secret" and stored.ready
    form = {"llm_enabled": "1", "llm_base_url": "http://x/v1/", "llm_model": "m",
            "llm_api_key": "", "llm_temperature": "9", "llm_max_tokens": "abc"}
    updated = config_from_form(form, stored)
    assert updated.api_key == "sk-secret"
    assert updated.base_url == "http://x/v1"
    assert updated.temperature == D("2")          # clamped
    assert updated.max_tokens == stored.max_tokens  # invalid -> unchanged
    assert updated.use_in_screen is False         # unticked checkbox
    assert config_from_form({**form, "llm_clear_api_key": "1"}, stored).api_key == ""


# ------------------------------------------------------------------- client

def _client(handler, **config) -> LLMClient:
    return LLMClient(LLMConfig(**{**CONFIG.__dict__, **config}),
                     transport=httpx.MockTransport(handler))


def test_client_posts_openai_shape_with_auth():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    assert _client(handler, api_key="k").chat([{"role": "user", "content": "hi"}]) == "{}"
    assert seen["url"] == "http://llm.local/v1/chat/completions"
    assert seen["auth"] == "Bearer k"
    assert seen["body"]["model"] == "test-model"
    assert seen["body"]["response_format"] == {"type": "json_object"}


def test_client_retries_without_json_mode_when_rejected():
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if "response_format" in body:
            return httpx.Response(400, text="response_format not supported")
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    assert _client(handler).chat([]) == "{}"
    assert len(bodies) == 2 and "response_format" not in bodies[1]


def test_client_maps_transport_failures_to_unreachable():
    def refuse(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(LLMUnreachable):
        _client(refuse).chat([])
    with pytest.raises(LLMUnreachable):
        _client(lambda r: httpx.Response(503, text="loading")).chat([])
    with pytest.raises(LLMError):
        _client(lambda r: httpx.Response(200, json={"oops": 1})).chat([])
    models = _client(lambda r: httpx.Response(200, json={"data": [{"id": "b"}, {"id": "a"}]}))
    assert models.list_models() == ["a", "b"]
