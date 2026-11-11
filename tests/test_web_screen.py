"""Screener page = the discovery scan; Settings page edits the funnel."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal as D

from app.db.base import session_scope
from app.db.models import AppSetting, ScanScore
from app.ingestion.ingest import ensure_ticker
from tests.test_web_pages import client  # noqa: F401 - shared fixture

DAY = dt.date(2026, 1, 5)


def _scan(rows):
    with session_scope() as s:
        for rank, (symbol, cap, has_fund) in enumerate(rows, start=1):
            t = ensure_ticker(s, symbol, in_universe=True, market_cap=D(cap),
                              name=f"{symbol} Inc.", sector="Industrials")
            s.add(ScanScore(ticker_id=t.id, scan_date=DAY, score=D("80") - rank, rank=rank,
                            has_fundamentals=has_fund,
                            sub_scores={"value": "70.5", "momentum": "60", "quality": None,
                                        "sentiment": None, "risk": "55"}))
        s.commit()


def test_screener_lists_the_scan_with_shortlist_and_size_filter(client):
    _scan([("GEM", "450000000", True), ("BIG", "900000000000", True),
           ("PRICEONLY", "3000000000", False)])
    html = client.get("/screen").text
    assert "Scanned <b>3</b>" in html
    for symbol in ("GEM", "BIG", "PRICEONLY"):
        assert f'href="/research/{symbol}"' in html
    assert "$450M" in html and "$900.0B" in html
    assert "70.5" in html and "n/a" in html, "unknown sub-score shows n/a, never 0"

    small = client.get("/screen?cap=small").text
    assert 'href="/research/GEM"' in small and 'href="/research/BIG"' not in small

    with session_scope() as s:
        s.add(AppSetting(key="scan_shortlist_size", value="1"))
        s.commit()
    short = client.get("/screen?view=shortlist").text
    assert 'href="/research/GEM"' in short and 'href="/research/BIG"' not in short


def test_screener_empty_state_before_the_first_scan(client):
    html = client.get("/screen").text
    assert "No scan yet" in html and "Nothing matches." in html


def test_settings_saves_discovery_floors_in_millions(client):
    resp = client.post("/settings/scan", data={
        "scan_min_market_cap_m": "500", "scan_min_price": "3",
        "scan_min_dollar_volume_m": "2", "scan_shortlist_size": "40",
        "scan_fundamentals_per_night": "300"}, follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/settings#discovery"
    with session_scope() as s:
        from app.research.scan import load_scan_config
        config = load_scan_config(s)
    assert config.min_market_cap == D("500000000") and config.shortlist_size == 40
    assert config.min_dollar_volume == D("2000000") and config.fundamentals_per_night == 300
    html = client.get("/settings").text
    assert 'id="discovery"' in html and 'value="500"' in html and 'value="40"' in html
