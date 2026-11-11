"""Web layer for the AI analyst: settings panel, key handling, report rendering."""

from __future__ import annotations

from decimal import Decimal as D

from app.db.base import session_scope
from app.db.models import ResearchReport
from app.ingestion.ingest import ensure_ticker
from app.research.llm import LLMError, load_llm_config
from tests.test_web_pages import client  # noqa: F401 - shared fixture

FORM = {"llm_enabled": "1", "llm_base_url": "http://host.docker.internal:11434/v1",
        "llm_model": "qwen2.5:32b", "llm_api_key": "sk-very-secret",
        "llm_temperature": "0.1", "llm_max_tokens": "2000",
        "llm_timeout_seconds": "240", "llm_use_in_screen": "1"}


def test_settings_save_persists_and_never_echoes_the_key(client):
    assert "off · rules engine" in client.get("/settings").text
    assert client.post("/settings/llm", data=FORM, follow_redirects=False).status_code == 303
    html = client.get("/settings").text
    assert "on · qwen2.5:32b" in html
    assert "sk-very-secret" not in html
    assert "set — blank keeps it" in html

    client.post("/settings/llm", data={**FORM, "llm_api_key": ""})
    with session_scope() as s:
        assert load_llm_config(s).api_key == "sk-very-secret"
    client.post("/settings/llm", data={**FORM, "llm_api_key": "", "llm_clear_api_key": "1"})
    with session_scope() as s:
        assert load_llm_config(s).api_key == ""


def test_connection_test_reports_success_and_failure(client, monkeypatch):
    class Ok:
        def __init__(self, config):
            self.config = config

        def list_models(self):
            return ["llama3.1:8b", "qwen2.5:32b"]

        def chat(self, messages):
            return '{"ok": true}'

        def close(self):
            pass

    class Down(Ok):
        def list_models(self):
            raise LLMError("cannot reach http://nowhere/v1")

        def chat(self, messages):
            raise LLMError("cannot reach http://nowhere/v1")

    monkeypatch.setattr("app.web.routes_settings.LLMClient", Ok)
    html = client.post("/settings/llm/test", data=FORM).text
    assert "Connected." in html and "2 model(s) served" in html and "{&#34;ok&#34;: true}" in html  # escaped

    html = client.post("/settings/llm/test", data={**FORM, "llm_model": "missing"}).text
    assert '"missing" is not among them' in html

    monkeypatch.setattr("app.web.routes_settings.LLMClient", Down)
    assert "Failed: cannot reach" in client.post("/settings/llm/test", data=FORM).text
    assert "Enter a base URL" in client.post(
        "/settings/llm/test", data={**FORM, "llm_base_url": ""}).text


def _report(payload: dict, version: str, summary: str) -> None:
    with session_scope() as s:
        t = ensure_ticker(s, "AIX")
        s.add(ResearchReport(ticker_id=t.id, model_or_rule_version=version, summary=summary,
                             overall_stance="bullish", score=D("71"), payload_json=payload))
        s.commit()


def test_report_shows_ai_analysis_with_rules_baseline(client):
    _report({"bull_points": ["ROE 25%"], "bear_points": [], "risks": [], "evidence": [],
             "sub_scores": [], "weights_used": {}, "missing_fields": [],
             "rules": {"score": "58", "stance": "neutral", "summary": "rules said meh"},
             "llm": {"model": "qwen2.5:32b", "notes": ["default stop used"],
                     "recommendation": {"action": "long", "confidence": "70",
                                        "horizon": "medium", "target_price": "130",
                                        "stop_price": None, "rationale": "trend + value"}}},
            "llm:qwen2.5:32b", "Model thesis here.")
    html = client.get("/research/AIX").text
    assert "AI analysis" in html and "qwen2.5:32b" in html
    assert "Model thesis here." in html and "trend + value" in html
    assert "composite 58/100" in html and "rules said meh" in html
    assert "note: default stop used" in html


def test_report_explains_rules_fallback(client):
    _report({"bull_points": [], "bear_points": [], "risks": [], "evidence": [],
             "sub_scores": [], "weights_used": {}, "missing_fields": [],
             "llm_error": "model did not answer within 180s"}, "rules-v1", "rules summary")
    html = client.get("/research/AIX").text
    assert "AI analysis failed" in html and "did not answer within 180s" in html
