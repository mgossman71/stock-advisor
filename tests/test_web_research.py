"""Research page: find any stock, research one the app doesn't track yet.

The discovery funnel only reports on its shortlist, so the page used to show
nothing for most stocks (TGT): search only filtered existing reports and an
unknown ticker sent you to the CLI.  These pin the fix.
"""

from __future__ import annotations

import datetime as dt

from app.db.base import session_scope
from app.db.models import ResearchReport, Ticker
from app.ingestion.ingest import ensure_ticker
from tests.conftest import FakeProvider, make_bar
from tests.test_web_pages import client  # noqa: F401 - shared fixture

DAY = dt.date(2026, 1, 5)


def _provider_with_bars(symbol: str) -> FakeProvider:
    return FakeProvider(bars=[make_bar(symbol, DAY - dt.timedelta(days=90 - i), str(100 + i))
                              for i in range(90)])


def test_search_finds_stocks_without_reports_by_symbol_and_name(client):
    with session_scope() as s:
        ensure_ticker(s, "TGT", name="Target Corporation", in_universe=True)
        ensure_ticker(s, "TGTX", name="TG Therapeutics", in_universe=True)
        s.commit()
    html = client.get("/research?q=TGT").text
    assert "Target Corporation" in html and "not researched yet" in html
    assert 'hx-post="/research/TGT/run"' in html and "Research now" in html
    assert html.index(">TGT<") < html.index(">TGTX<")  # exact match first
    assert "Research TGT now" not in html  # already tracked: no "add" panel
    assert "Target Corporation" in client.get("/research?q=target").text


def test_untracked_symbol_offers_research_now(client):
    html = client.get("/research?q=tgt").text
    assert "TGT isn't in the app's stock list yet" in html
    assert 'hx-post="/research/TGT/run"' in html
    page = client.get("/research/TGT")
    assert page.status_code == 404
    assert "TGT isn't tracked yet" in page.text and "Research TGT now" in page.text
    assert "app.cli fetch" not in page.text  # no more "go use the terminal"
    # Free text that can't be a symbol (a company name search) offers no "add".
    assert "isn't in the app's stock list yet" not in client.get("/research?q=not a symbol!").text


def test_research_now_fetches_and_reports_an_untracked_stock(client, monkeypatch):
    monkeypatch.setattr("app.web.routes_research.build_provider",
                        lambda settings: _provider_with_bars("TGT"))
    resp = client.post("/research/tgt/run")
    assert resp.headers["HX-Redirect"] == "/research/TGT"
    with session_scope() as s:
        ticker = s.query(Ticker).filter_by(symbol="TGT").one()
        assert s.query(ResearchReport).filter_by(ticker_id=ticker.id).count() == 1
    assert client.get("/research/TGT").status_code == 200


def test_unknown_symbol_with_no_data_is_not_kept(client, monkeypatch):
    monkeypatch.setattr("app.web.routes_research.build_provider",
                        lambda settings: FakeProvider())
    resp = client.post("/research/ZZZZQ/run")
    assert resp.headers["HX-Redirect"] == "/research?q=ZZZZQ&err=nodata"
    with session_scope() as s:
        assert s.query(Ticker).filter_by(symbol="ZZZZQ").count() == 0
    assert "No price data found for ZZZZQ" in client.get(resp.headers["HX-Redirect"]).text


def test_invalid_symbol_is_refused_before_any_fetch(client, monkeypatch):
    monkeypatch.setattr("app.web.routes_research.build_provider",
                        lambda settings: (_ for _ in ()).throw(AssertionError("fetched")))
    resp = client.post("/research/%3Cscript%3E/run")
    assert resp.headers["HX-Redirect"].endswith("err=badsymbol")
