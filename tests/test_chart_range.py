"""Chart range: a global default, a per-view override that is never saved."""

from __future__ import annotations

import datetime as dt
import json
import re
from decimal import Decimal as D

from app.db.base import session_scope
from app.db.models import PriceHistory, ResearchReport
from app.ingestion.ingest import ensure_ticker
from app.web.display import DEFAULT_RANGE, RANGES, slice_range
from tests.test_web_pages import client  # noqa: F401 - shared fixture

LAST = dt.date(2026, 11, 11)


def _seed(days: int = 1900) -> None:
    """~5 years of daily closes ending LAST, plus a report so the chart renders."""
    with session_scope() as s:
        t = ensure_ticker(s, "HIST")
        for i in range(days):
            s.add(PriceHistory(ticker_id=t.id, date=LAST - dt.timedelta(days=i),
                               close=D("100"), source="fake"))
        s.add(ResearchReport(ticker_id=t.id, model_or_rule_version="rules-v1", summary="s",
                             overall_stance="neutral", score=D("50"),
                             payload_json={"bull_points": [], "bear_points": [], "risks": [],
                                           "evidence": [], "sub_scores": [],
                                           "weights_used": {}, "missing_fields": []}))
        s.commit()


def _chart(html: str) -> tuple[list[dict], str]:
    series = json.loads(re.search(r"const series = (\[.*?\]);", html, re.S).group(1))
    active = re.search(r'<a class="btn " href="\?range=(\w+)"\s+aria-current', html).group(1)
    return series, active


def test_slice_counts_back_from_the_newest_bar():
    bars = [(LAST - dt.timedelta(days=i), D("1")) for i in range(400)][::-1]
    assert slice_range(bars, "1m")[0][0] == LAST - dt.timedelta(days=31)
    assert len(slice_range(bars, "max")) == 400
    assert len(slice_range(bars, "5y")) == 400  # asks for more than stored: all of it
    assert slice_range([], "1y") == []


def test_first_view_uses_the_global_default(client):
    _seed()
    series, active = _chart(client.get("/research/HIST").text)
    assert active == DEFAULT_RANGE == "1y"
    assert series[0]["date"] == (LAST - dt.timedelta(days=365)).isoformat()


def test_override_is_for_this_view_only(client):
    _seed()
    series, active = _chart(client.get("/research/HIST?range=5y").text)
    assert active == "5y" and len(series) > 1800
    # Coming back without the parameter starts from the default again.
    _, active = _chart(client.get("/research/HIST").text)
    assert active == DEFAULT_RANGE


def test_changing_the_global_default(client):
    _seed()
    assert 'id="display"' in client.get("/settings").text
    client.post("/settings/display", data={"chart_default_range": "3m"})
    series, active = _chart(client.get("/research/HIST").text)
    assert active == "3m" and len(series) < 100
    assert 'value="3m" selected' in client.get("/settings").text
    client.post("/settings/display", data={"chart_default_range": "bogus"})  # ignored
    assert _chart(client.get("/research/HIST").text)[1] == "3m"


def test_bad_override_falls_back_to_the_default(client):
    _seed()
    _, active = _chart(client.get("/research/HIST?range=17y").text)
    assert active == DEFAULT_RANGE


def test_every_range_has_a_button_and_the_default_is_starred(client):
    _seed()
    html = client.get("/research/HIST").text
    for key, (label, _) in RANGES.items():
        assert f'href="?range={key}"' in html
    assert "1Y ★" in html and "History stored from" in html
