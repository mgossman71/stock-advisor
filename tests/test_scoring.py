"""Scoring rules pinned: thresholds, missing-data exclusion, weight handling.

These tests *are* the "how the scoring works" documentation the README must
restate - if you change a threshold here, change the README too.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal as D

from app.db.models import Fundamentals
from app.research.scoring import (
    DEFAULT_WEIGHTS, composite, ensure_default_weights, load_weights, momentum_score,
    quality_score, risk_score, sentiment_score, value_score,
)
from app.research.indicators import pct_below



def _fund(**overrides) -> Fundamentals:
    base = dict(
        ticker_id=1, as_of_date=date(2026, 1, 5), source="fake",
        pe_trailing=D("20"), ev_ebitda=D("15"), ps=D("3"), pb=D("2"),
        fcf_yield=D("4"), profit_margin=D("10"), return_on_equity=D("15"),
        debt_to_equity=D("1.0"), revenue_growth_yoy=D("10"), eps_growth_yoy=D("10"),
    )
    base.update(overrides)
    return Fundamentals(**base)  # unsaved row: scoring never needs a DB


# ---------------------------------------------------------------- thresholds

def test_value_score_linear_maps():
    cheap = _fund(pe_trailing=D("10"), ev_ebitda=D("8"), ps=D("0.5"), pb=D("0.5"),
                  fcf_yield=D("8"))
    rich = _fund(pe_trailing=D("40"), ev_ebitda=D("25"), ps=D("12"), pb=D("8"),
                 fcf_yield=D("0"))
    vs_cheap = value_score(cheap, D("20"))
    vs_rich = value_score(rich, D("20"))
    by_name = {c.name: c.score for c in vs_cheap.components}
    assert by_name["pe_vs_sector"] == D("75.00")  # 10 vs median 20: half median -> 100, median -> 50
    assert by_name["ev_ebitda"] == D("100.00")
    assert by_name["fcf_yield"] == D("100.00")
    rich_by_name = {c.name: c.score for c in vs_rich.components}
    assert rich_by_name["pe_vs_sector"] == D("0")  # 40 = 2x median -> 0 points
    assert vs_cheap.score > vs_rich.score


def test_negative_pe_is_not_cheap():
    vs = value_score(_fund(pe_trailing=D("-8")), D("20"))
    comp = next(c for c in vs.components if c.name == "pe_vs_sector")
    assert comp.score == D("0")
    assert "not profitable" in comp.note


def test_negative_multiples_are_excluded_not_cheap():
    """Negative P/B (negative book value) must not score as 'very cheap'."""
    vs = value_score(_fund(pb=D("-113.34"), ev_ebitda=D("-4")), None)
    pb = next(c for c in vs.components if c.name == "pb")
    ev = next(c for c in vs.components if c.name == "ev_ebitda")
    assert pb.score is None and "not meaningful" in pb.note
    assert ev.score is None and "not meaningful" in ev.note
    assert "pb" in vs.missing and "ev_ebitda" in vs.missing
    # A 0.004 P/B reads as "0.00" - broken data, not a deep-value signal.
    tiny = value_score(_fund(pb=D("0.004")), None)
    tiny_pb = next(c for c in tiny.components if c.name == "pb")
    assert tiny_pb.score is None and "not meaningful" in tiny_pb.note


def test_missing_value_fields_excluded_not_zeroed():
    bare = _fund(pe_trailing=None, ev_ebitda=None, ps=None, pb=None, fcf_yield=None)
    vs = value_score(bare, D("20"))
    assert vs.score is None
    assert len(vs.missing) == 5


def test_composite_renormalises_over_missing_blocks():
    fund = _fund(pe_trailing=D("12"), ev_ebitda=D("9"), ps=D("1"), pb=D("1"),
                 fcf_yield=D("6"), profit_margin=D("18"), return_on_equity=D("22"),
                 debt_to_equity=D("0.4"), revenue_growth_yoy=D("12"),
                 eps_growth_yoy=D("15"))
    good_value = value_score(fund, None)
    good_quality = quality_score(fund)
    empty_momentum = momentum_score({k: None for k in ("1m", "3m", "6m", "12m")},
                                    None, None, None, None)
    empty_sent = sentiment_score(None, None, None, None)
    empty_risk = risk_score(None, None, None, None)
    score = composite([good_value, empty_momentum, good_quality, empty_sent, empty_risk],
                      DEFAULT_WEIGHTS)
    assert score is not None
    # Only value (0.30) + quality (0.25) contribute; result sits between them.
    assert min(good_value.score, good_quality.score) <= score <= max(
        good_value.score, good_quality.score
    )
    all_empty = composite([empty_momentum, empty_sent, empty_risk], DEFAULT_WEIGHTS)
    assert all_empty is None  # unrateable stays unrateable, never 50


def test_momentum_maps_returns_and_sma_trend():
    ms = momentum_score({"1m": D("10"), "3m": D("-20"), "6m": D("0"), "12m": D("20")},
                        rsi_value=D("55"), dist_from_high=D("-5"),
                        above_50=True, above_200=True)
    by_name = {c.name: c.score for c in ms.components}
    assert by_name["return_1m"] == D("75.00")   # +10 of [-20,+20] band
    assert by_name["return_3m"] == D("0")       # floor
    assert by_name["rsi14"] == D("55.00")
    assert by_name["trend_vs_sma"] == D("100")
    # RSI 90 is overheated: penalised below a 70 RSI.
    hot = momentum_score({k: None for k in ("1m", "3m", "6m", "12m")}, D("90"),
                         None, None, None)
    cool = momentum_score({k: None for k in ("1m", "3m", "6m", "12m")}, D("70"),
                          None, None, None)
    hot_rsi = next(c for c in hot.components if c.name == "rsi14").score
    cool_rsi = next(c for c in cool.components if c.name == "rsi14").score
    assert hot_rsi < cool_rsi


def test_sentiment_net_actions_and_target_distance():
    ss = sentiment_score(upgrades=5, downgrades=1, target_upside_pct=D("10"),
                         news_avg_sentiment=None)
    by_name = {c.name: c.score for c in ss.components}
    assert by_name["analyst_actions_90d"] == D("90.00")  # 50 + net(+4)*10
    assert by_name["target_upside"] == D("75.00")  # +10 of [-20,+20]
    assert by_name["news_tone"] is None  # excluded, not 0


def test_risk_score_is_inverse_safety():
    safe = risk_score(vol_pct=D("15"), drawdown_pct=D("-5"),
                      last_surprise_pct=D("3"), short_pct_float=D("1"))
    danger = risk_score(vol_pct=D("45"), drawdown_pct=D("-40"),
                        last_surprise_pct=D("-8"), short_pct_float=D("15"))
    assert safe.score >= D("90")
    # -8 % surprise still maps to 13.3 points of the risk block; the composite
    # stays near the floor: <= 5/100.
    assert danger.score <= D("5")


# ---------------------------------------------------------------- weights DB

def test_weights_seeded_once_and_loaded(session):
    ensure_default_weights(session)
    ensure_default_weights(session)  # idempotent
    from app.db.models import ScoringWeight

    assert session.query(ScoringWeight).count() == 5
    weights = load_weights(session)
    assert sum(weights.values()) == D("1.00") or sum(weights.values()) == D("1")
    assert weights["value"] == DEFAULT_WEIGHTS["value"]


def test_percentile_helper():
    series = [D("10"), D("20"), D("30"), D("40")]
    assert pct_below(series, D("20")) == D("50")
    assert pct_below([], D("1")) is None
