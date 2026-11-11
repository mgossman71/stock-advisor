"""Advisor engine: when it fires, what it cites, and idempotency of re-runs."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal as D

import pytest

from app.db.models import AnalystAction, Fundamentals, PriceHistory, Recommendation
from app.ingestion.ingest import ensure_ticker
from app.research.engine import RuleBasedAdvisor
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



@pytest.fixture
def advisor(session):
    ensure_default_weights(session)
    return RuleBasedAdvisor()


def _latest(session, symbol: str) -> D:
    t = ensure_ticker(session, symbol)
    row = session.query(PriceHistory).filter(PriceHistory.ticker_id == t.id) \
        .order_by(PriceHistory.date.desc()).first()
    return row.close


def test_bullish_report_emits_one_long_at_stored_price(session, advisor):
    _seed_bars(session, "WINNER", 300, D("100"), D("0.25"))
    _seed_strong_fundamentals(session, "WINNER")
    report = advisor.research(session, "WINNER")
    recs = advisor.recommendations_for(session, report)
    assert len(recs) == 1
    rec = recs[0]
    assert rec.direction == "long"
    assert rec.entry_price_at_call == _latest(session, "WINNER")
    assert rec.stop_price == rec.entry_price_at_call * D("0.85")
    assert D("10") <= rec.confidence <= D("95")
    assert rec.evidence_json, "call must cite the data behind it"
    assert rec.engine_version == "rules-v1"

    # Re-running the nightly screen cannot spam a second open call.
    report2 = advisor.research(session, "WINNER")
    assert advisor.recommendations_for(session, report2) == []
    assert session.query(Recommendation).filter_by(status="open").count() == 1


def test_bearish_report_emits_short(session, advisor):
    _seed_bars(session, "LOSER", 300, D("200"), D("-0.6"))
    ticker = ensure_ticker(session, "LOSER")
    session.add(Fundamentals(
        ticker_id=ticker.id, as_of_date=DAY0 + dt.timedelta(days=300), source="fake",
        pe_trailing=D("60"), ev_ebitda=D("30"), ps=D("15"), pb=D("9"),
        profit_margin=D("-5"), return_on_equity=D("-10"), debt_to_equity=D("3"),
        revenue_growth_yoy=D("-8"), eps_growth_yoy=D("-20"), fcf_yield=D("-2"),
    ))
    session.flush()
    report = advisor.research(session, "LOSER")
    recs = advisor.recommendations_for(session, report)
    assert len(recs) == 1 and recs[0].direction == "short"
    assert recs[0].stop_price == recs[0].entry_price_at_call * D("1.15")


def test_no_price_no_call_even_with_a_report(session, advisor):
    """A fundamentals-only ticker must not get an entry price out of thin air."""
    ticker = ensure_ticker(session, "BARE")
    session.add(Fundamentals(
        ticker_id=ticker.id, as_of_date=dt.date.today(), source="fake",
        pe_trailing=D("10"), profit_margin=D("30"),
    ))
    session.flush()
    report = advisor.research(session, "BARE")
    assert report is not None
    assert advisor.recommendations_for(session, report) == []


def test_neutral_zone_emits_nothing(session, advisor):
    """Sideways choppy prices + mid fundamentals: score sits in the neutral band."""
    closes = [D(str(100 + (1 if i % 4 < 2 else -1))) for i in range(300)]
    ticker = ensure_ticker(session, "FLAT")
    for i, price in enumerate(closes):
        session.add(PriceHistory(ticker_id=ticker.id, date=DAY0 + dt.timedelta(days=i),
                                 close=price, adj_close=price, source="fake"))
    session.add(Fundamentals(
        ticker_id=ticker.id, as_of_date=DAY0 + dt.timedelta(days=300), source="fake",
        pe_trailing=D("22"), ev_ebitda=D("16"), ps=D("4"), pb=D("3"),
        profit_margin=D("10"), return_on_equity=D("12"), debt_to_equity=D("1.2"),
        revenue_growth_yoy=D("3"), eps_growth_yoy=D("2"), fcf_yield=D("2"),
    ))
    session.flush()
    report = advisor.research(session, "FLAT")
    assert report.overall_stance == "neutral", (report.summary, report.score)
    assert advisor.recommendations_for(session, report) == []


def test_overextended_short_fires_on_net_downgrades_and_rich_price(advisor):
    """Regression: the rule parsed the wrong string and could never fire."""
    payload = {"indicators": {"analyst_upgrades_90d": 0, "analyst_downgrades_90d": 3,
                              "target_upside_pct": "-14.5"}}
    assert advisor._overextended(payload) == D("-14.50")
    # One net downgrade short of the threshold -> no setup.
    payload["indicators"]["analyst_upgrades_90d"] = 2
    assert advisor._overextended(payload) is None
    # Price within 10 % of target -> no setup.
    assert advisor._overextended({"indicators": {
        "analyst_upgrades_90d": 0, "analyst_downgrades_90d": 5,
        "target_upside_pct": "-5"}}) is None


def test_overextended_short_reads_reports_saved_before_the_indicator_keys(advisor):
    from app.research.scoring import sentiment_score

    ss = sentiment_score(upgrades=0, downgrades=5, target_upside_pct=D("-25"),
                         news_avg_sentiment=None)
    legacy = {"sub_scores": [ss.as_dict()],
              "evidence": [c.evidence.as_dict() for c in ss.components if c.evidence]}
    assert advisor._overextended(legacy) == D("-25.00")
