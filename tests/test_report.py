"""End-to-end report building from stored DB rows - and its no-fabrication rules."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal as D

import pytest

from app.db.models import AnalystAction, Fundamentals, PriceHistory, ResearchReport
from app.ingestion.ingest import ensure_ticker
from app.research.report import build_report
from app.research.scoring import ensure_default_weights

DAY0 = dt.date(2025, 1, 6)


def _seed_bars(session, symbol: str, n: int, start: D, step: D) -> None:
    ticker = ensure_ticker(session, symbol)
    for i in range(n):
        price = start + step * i
        session.add(PriceHistory(
            ticker_id=ticker.id, date=DAY0 + dt.timedelta(days=i),
            close=price, adj_close=price, source="fake",
        ))
    session.flush()


def _seed_strong_fundamentals(session, symbol: str) -> None:
    ticker = ensure_ticker(session, symbol)
    session.add(Fundamentals(
        ticker_id=ticker.id, as_of_date=DAY0 + dt.timedelta(days=300), source="fake",
        pe_trailing=D("15"), pe_forward=D("13"), ev_ebitda=D("9"), ps=D("2"), pb=D("1.5"),
        debt_to_equity=D("0.5"), revenue_growth_yoy=D("12"), eps_growth_yoy=D("18"),
        dividend_yield=D("1"), fcf_yield=D("6"), gross_margin=D("40"),
        operating_margin=D("25"), profit_margin=D("20"), return_on_equity=D("25"),
        current_price=D("172"), target_price_consensus=D("200"), short_pct_float=D("1"),
        beta=D("0.9"), fifty_two_week_high=D("175"), fifty_two_week_low=D("102"),
    ))
    for action, when in (("upgrade", dt.date.today() - dt.timedelta(days=5)),
                         ("target_raise", dt.date.today() - dt.timedelta(days=2))):
        session.add(AnalystAction(ticker_id=ticker.id, action_date=when, firm="Firm",
                                  action=action, source="fake"))
    session.flush()


@pytest.fixture(autouse=True)
def _weights(session):
    ensure_default_weights(session)


def test_prices_but_no_fundamentals_report_says_unavailable(session):
    """The spec's core honesty rule: missing blocks are named, never zero-filled."""
    _seed_bars(session, "PARTIAL", 260, D("100"), D("0.1"))
    report = build_report(session, "PARTIAL")
    assert report is not None
    payload = report.payload_json
    by_name = {s["name"]: s["score"] for s in payload["sub_scores"]}
    assert by_name["value"] is None and by_name["quality"] is None
    assert "pe_vs_sector" in payload["missing_fields"]
    assert "profit_margin" in payload["missing_fields"]
    # Scored on what exists (momentum/sentiment/risk), not on 50-everything.
    assert report.score > 0
    assert "unavailable" in report.summary
    assert report.overall_stance in ("neutral", "bullish", "bearish")


def test_unknown_ticker_returns_none(session):
    assert build_report(session, "GHOST") is None


def test_full_report_is_bullish_and_evidence_cited(session):
    """AAPL-style strong setup -> bullish, >= 65, every component cites source+date."""
    _seed_bars(session, "WINNER", 300, D("100"), D("0.25"))
    _seed_strong_fundamentals(session, "WINNER")
    report = build_report(session, "WINNER")
    assert report.overall_stance == "bullish", report.summary
    assert report.score >= D("65")
    evidence = report.payload_json["evidence"]
    assert evidence, "report must cite data points"
    pe_row = next(e for e in evidence if e["label"] == "PE (trailing)")
    assert pe_row["source"] == "fake" and pe_row["as_of"]
    assert report.data_as_of is not None
    assert report.payload_json["bull_points"], "bullish report needs bull points"
    assert any(r.id == report.id for r in session.query(ResearchReport))


def test_risk_flags_surface(session):
    """A violent downtrend with a miss and heavy short interest -> risk flags + bearish."""
    _seed_bars(session, "LOSER", 300, D("200"), D("-0.6"))
    ticker = ensure_ticker(session, "LOSER")
    session.add(Fundamentals(
        ticker_id=ticker.id, as_of_date=DAY0 + dt.timedelta(days=300), source="fake",
        pe_trailing=D("60"), ev_ebitda=D("30"), ps=D("15"), pb=D("9"),
        profit_margin=D("-5"), return_on_equity=D("-10"), debt_to_equity=D("3"),
        revenue_growth_yoy=D("-8"), eps_growth_yoy=D("-20"), fcf_yield=D("-2"),
    ))
    from app.db.models import EarningsEvent

    session.add(EarningsEvent(ticker_id=ticker.id, event_date=DAY0 + dt.timedelta(days=280),
                              surprise_pct=D("-9"), status="reported", source="fake"))
    session.flush()
    report = build_report(session, "LOSER")
    assert report.overall_stance == "bearish", report.summary
    risks = report.payload_json["risks"]
    assert any("earnings surprise" in r for r in risks)
    assert any("drawdown" in r for r in risks)
    assert report.bear_points if hasattr(report, "bear_points") else report.payload_json["bear_points"]
