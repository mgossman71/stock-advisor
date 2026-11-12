"""Emerging-opportunity lens in the UI: dashboard panel, screener, filters."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal as D

from app.db.base import session_scope
from app.db.models import PriceHistory, Recommendation, ScanScore
from app.ingestion.ingest import ensure_ticker
from app.research.scan import load_scan_config
from tests.test_web_pages import client  # noqa: F401 - shared fixture

DAY = dt.date(2026, 6, 1)


def _seed() -> None:
    """Two scanned stocks: ROCKT is a strong emerging candidate with an open
    emerging-v1 call; SLOW scores low on the lens."""
    with session_scope() as s:
        for symbol, rank, lens, lens_rank, reasons in (
                ("ROCKT", 40, "88", 1, ["broke above its 20-day high on heavy volume",
                                        "relative strength in the top 3% of the market"]),
                ("SLOW", 1, "20", 2, [])):
            t = ensure_ticker(s, symbol, name=f"{symbol} Inc", in_universe=True,
                              asset_class="stock")
            s.add(ScanScore(ticker_id=t.id, scan_date=DAY, score=D("60"), rank=rank,
                            has_fundamentals=True, sub_scores={}, emerging_score=D(lens),
                            emerging_rank=lens_rank, emerging={"reasons": reasons}))
            s.add(PriceHistory(ticker_id=t.id, date=DAY, close=D("50"), source="fake"))
        rocket = ensure_ticker(s, "ROCKT")
        s.add(Recommendation(
            ticker_id=rocket.id, direction="long", horizon="short",
            generated_at=dt.datetime(2026, 6, 1, tzinfo=dt.timezone.utc),
            entry_price_at_call=D("50"), stop_price=D("46"), target_price=D("62.5"),
            status="open", confidence=D("73"), rationale="Emerging: breakout",
            evidence_json=[], engine_version="emerging-v1"))
        slow = ensure_ticker(s, "SLOW")
        s.add(Recommendation(
            ticker_id=slow.id, direction="long", horizon="medium",
            generated_at=dt.datetime(2026, 6, 1, tzinfo=dt.timezone.utc),
            entry_price_at_call=D("50"), stop_price=D("42"), target_price=D("60"),
            status="open", confidence=D("66"), rationale="core call",
            evidence_json=[], engine_version="rules-v1"))
        s.commit()


def test_dashboard_lists_early_movers_with_reasons_and_actions(client):
    _seed()
    html = client.get("/").text
    panel = html[html.index('id="early-movers"'):]
    panel = panel[:panel.index("</table>")]
    assert "ROCKT" in panel and "broke above its 20-day high" in panel
    assert "SLOW" not in panel  # below the lens's shortlist bar
    assert "stop 46.00" in panel and 'href="/portfolio?prefill=' in panel
    assert "none graded yet" in html


def test_screener_emerging_column_view_and_sort(client):
    _seed()
    html = client.get("/screen").text
    assert "<th class=\"num\">Emerging</th>" in html and "▲" in html
    assert html.index(">SLOW<") < html.index(">ROCKT<")  # default: scan rank
    by_lens = client.get("/screen?sort=emerging").text
    assert by_lens.index(">ROCKT<") < by_lens.index(">SLOW<")
    only = client.get("/screen?view=emerging").text
    assert ">ROCKT<" in only and ">SLOW<" not in only


def test_recommendations_filter_by_lens(client):
    _seed()
    emerging = client.get("/recommendations?lens=emerging").text
    assert "Emerging: breakout" in emerging and "core call" not in emerging
    assert "emerging-opportunity calls only" in emerging
    core = client.get("/recommendations?lens=core").text
    assert "core call" in core and "Emerging: breakout" not in core
    assert "core call" in client.get("/recommendations?lens=bogus").text  # ignored


def test_emerging_shortlist_size_is_a_setting(client):
    assert 'name="scan_emerging_size"' in client.get("/settings").text
    client.post("/settings/scan", data={"scan_emerging_size": "4"})
    with session_scope() as s:
        assert load_scan_config(s).emerging_size == 4
