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


def test_short_ranges_come_first_and_use_live_intraday_bars(client, monkeypatch):
    """1D and 1W sit before 1M and draw live intraday points (never stored)."""
    from app.db.models import PriceHistory as PH
    from app.providers.base import IntradayBar, ProviderResult
    from app.web.display import INTRADAY

    assert list(RANGES)[:3] == ["1d", "1w", "1m"]
    assert [RANGES[k][0] for k in ("1d", "1w")] == ["1D", "1W"]
    _seed()
    asked = []
    ny = dt.timezone(dt.timedelta(hours=-5))

    class Intraday:
        def get_intraday(self, symbol, *, period, interval):
            asked.append((symbol, period, interval))
            bars = [IntradayBar(symbol, dt.datetime(2026, 11, 11, 9, 30 + i * 5, tzinfo=ny),
                                D("100") + i, "fake") for i in range(3)]
            return ProviderResult("fake", "intraday", ok=True, data=tuple(bars))

    monkeypatch.setattr("app.web.routes_research.quote_provider", lambda settings: Intraday())
    with session_scope() as s:
        before = s.query(PH).count()
    html = client.get("/research/HIST?range=1d").text
    series, active = _chart(html)
    assert active == "1d" and asked == [("HIST", *INTRADAY["1d"])]
    assert [p["date"] for p in series] == ["09:30", "09:35", "09:40"]
    assert [p["close"] for p in series] == [100, 101, 102]
    _chart(client.get("/research/HIST?range=1w").text)
    assert asked[-1] == ("HIST", *INTRADAY["1w"])
    with session_scope() as s:
        assert s.query(PH).count() == before  # display only


def test_intraday_failure_falls_back_to_daily_closes(client, monkeypatch):
    from app.providers.base import ProviderResult

    _seed()

    class Down:
        def get_intraday(self, symbol, *, period, interval):
            return ProviderResult("fake", "intraday", ok=False, error="HTTP 429")

    monkeypatch.setattr("app.web.routes_research.quote_provider", lambda settings: Down())
    html = client.get("/research/HIST?range=1w").text
    series, active = _chart(html)
    assert active == "1w" and series[-1]["date"] == LAST.isoformat() and len(series) == 8
    assert "intraday prices unavailable (HTTP 429)" in html
