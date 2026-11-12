"""Emerging-opportunity lens: what it rewards, what it penalises, and the
calls it leads to.

Synthetic histories with known shapes: a clean breakout from a tight base on
heavy volume, a steady laggard, a downtrend and an over-extended spike.
Nothing here touches the network or an LLM.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal as D

import pytest

from app.db.models import PriceHistory, Recommendation, ResearchReport, ScanScore
from app.ingestion.ingest import ensure_ticker
from app.research import emerging
from app.research.emerging import Bars
from app.research.outcomes import scoreboard, update_statuses
from app.research.scan import (
    emerging_candidates, emerging_symbols, load_scan_config, run_scan, save_scan_config,
)
from app.research.screener import emerging_recommendation

TODAY = dt.date(2026, 6, 1)
N = 300


# ------------------------------------------------------------ shapes

def breakout() -> list[tuple[D, int]]:
    """Steady uptrend, a quiet tight base, then a breakout on 3x volume."""
    out = []
    for i in range(N):
        if i < 240:
            price, vol = D("50") + D("30") * i / 240, 1_000_000 + (200_000 if i % 2 else 0)
        elif i < 296:
            price, vol = D("80") + (D("0.3") if i % 2 else D("-0.3")), 700_000
        else:
            price, vol = D("81.5") + D("1.5") * (i - 296), 3_000_000
        out.append((price, vol))
    return out


def laggard(slope: str = "0.01") -> list[tuple[D, int]]:
    return [(D("40") + D(slope) * i + (D("0.4") if i % 2 else D("0")), 1_000_000)
            for i in range(N)]


def downtrend() -> list[tuple[D, int]]:
    return [(D("120") - D("0.25") * i, 1_000_000) for i in range(N)]


def spike() -> list[tuple[D, int]]:
    """Flat for months, then +60% in three weeks: late, over-extended."""
    return [(D("30") if i < 280 else D("30") * (D("1") + D("0.03") * (i - 279)), 1_000_000)
            for i in range(N)]


def as_bars(shape: list[tuple[D, int]]) -> Bars:
    closes = [p for p, _ in shape]
    return Bars(closes=closes, raw_closes=closes, highs=[p * D("1.005") for p in closes],
                lows=[p * D("0.995") for p in closes], volumes=[v for _, v in shape])


def store(session, symbol: str, shape: list[tuple[D, int]]):
    ticker = ensure_ticker(session, symbol, in_universe=True, asset_class="stock")
    for i, (price, vol) in enumerate(shape):
        session.add(PriceHistory(
            ticker_id=ticker.id, date=TODAY - dt.timedelta(days=N - 1 - i), close=price,
            adj_close=price, high=price * D("1.005"), low=price * D("0.995"), volume=vol,
            source="fake"))
    session.flush()
    return ticker


# ------------------------------------------------------------ the lens itself

def test_breakout_scores_high_with_its_reasons():
    sig = emerging.signals(as_bars(breakout()))
    assert sig["trend"] and sig["breakout"] and sig["tightness"] < D("0.5")
    points, reasons = emerging.score(sig, rs_percentile=D("95"))
    assert points >= emerging.CALL_MIN_SCORE
    text = " | ".join(reasons)
    for expected in ("relative strength in the top 5%", "uptrend", "52-week high",
                     "broke above its 20-day high", "volume"):
        assert expected in text, expected


def test_downtrend_and_late_spikes_are_penalised():
    down_points, down_reasons = emerging.score(emerging.signals(as_bars(downtrend())), D("5"))
    assert down_points < D("20")
    assert any("below its 200-day average" in r for r in down_reasons)
    spike_sig = emerging.signals(as_bars(spike()))
    _, spike_reasons = emerging.score(spike_sig, D("99"))
    assert any("may be late" in r for r in spike_reasons)


def test_too_little_history_is_not_scored():
    assert emerging.signals(as_bars(breakout()[:59])) is None


def test_unknown_volume_is_unknown_not_zero():
    bars = as_bars(breakout())
    bars.volumes = [None] * len(bars.volumes)
    sig = emerging.signals(bars)
    assert sig["vol_ratio"] is None and sig["updown"] is None and sig["breakout"] is False


def test_percentiles_rank_the_universe():
    pct = emerging.percentiles({1: D("-10"), 2: D("5"), 3: D("40"), 4: D("5")})
    assert pct[3] == D("100.0") and pct[1] == D("0.0") and pct[2] == pct[4]
    assert emerging.percentiles({7: D("1")}) == {7: D("100")}


def test_size_tilt_favours_small_and_mid_caps():
    def points(cap):
        return emerging.score(emerging.signals(as_bars(laggard()), market_cap=cap), D("50"))
    small, mid, large, unknown = (points(D("8e8")), points(D("5e9")), points(D("3e11")),
                                  points(None))
    assert small[0] - large[0] == D("5") and mid[0] - large[0] == D("3")
    assert unknown[0] == large[0]  # unknown size earns nothing, costs nothing
    assert any("small cap" in r for r in small[1])


def test_base_effect_growth_is_described_not_quoted():
    sig = emerging.signals(as_bars(laggard()), eps_growth=D("2282"))
    _, reasons = emerging.score(sig, D("50"))
    assert "EPS growth over 200% a year (from a low base)" in reasons


def test_growth_counts_only_when_known_and_strong():
    sig = emerging.signals(as_bars(laggard()), revenue_growth=D("40"), eps_growth=None)
    _, reasons = emerging.score(sig, D("50"))
    assert any("revenue growing 40%" in r for r in reasons)
    assert not any("EPS" in r for r in reasons)


# ------------------------------------------------------------ in the scan

@pytest.fixture
def scanned(session):
    store(session, "BRKO", breakout())
    for i in range(6):
        store(session, f"LAG{i}", laggard(slope=f"0.0{i}"))
    store(session, "DOWN", downtrend())
    result = run_scan(session, today=TODAY)
    session.flush()
    return result


def test_scan_ranks_the_breakout_first_and_shortlists_it(session, scanned):
    rows = {r.ticker.symbol: r for r in session.query(ScanScore)}
    top = rows["BRKO"]
    assert top.emerging_rank == 1 and top.emerging_score >= emerging.CALL_MIN_SCORE
    assert top.emerging["breakout"] is True and top.emerging["reasons"]
    assert rows["DOWN"].emerging_score < rows["LAG5"].emerging_score
    assert scanned.emerging[0] == "BRKO"
    assert emerging_symbols(session)[0] == "BRKO"
    assert "DOWN" not in emerging_symbols(session)  # below the shortlist bar
    assert emerging_candidates(session)[0][1].symbol == "BRKO"


def test_emerging_shortlist_size_zero_turns_it_off(session, scanned):
    config = load_scan_config(session)
    save_scan_config(session, type(config)(**{**config.__dict__, "emerging_size": 0}))
    assert emerging_symbols(session) == []


def _report(session, symbol: str, stance: str, *, llm: bool = False) -> ResearchReport:
    ticker = ensure_ticker(session, symbol)
    payload = {"evidence": []}
    if llm:
        payload["llm"] = {"model": "test-model", "recommendation": {}, "notes": []}
    report = ResearchReport(ticker_id=ticker.id, summary="AI thesis here.",
                            overall_stance=stance, score=D("60"), payload_json=payload,
                            model_or_rule_version="llm:test-model" if llm else "rules-v1")
    session.add(report)
    session.flush()
    return report


def test_emerging_call_levels_horizon_and_engine(session, scanned):
    rec = emerging_recommendation(session, _report(session, "BRKO", "neutral"))
    entry = rec.entry_price_at_call
    assert rec.direction == "long" and rec.horizon == "short"
    assert rec.stop_price == entry * D("0.92") and rec.target_price == entry * D("1.25")
    assert rec.engine_version == "emerging-v1"
    assert rec.rationale.startswith("Emerging: ") and "broke above" in rec.rationale
    assert D("55") <= rec.confidence <= D("85")
    # One open long per ticker: a second pass does not duplicate it.
    assert emerging_recommendation(session, _report(session, "BRKO", "neutral")) is None


@pytest.mark.parametrize("stance,llm", [("bearish", False), ("neutral", True), ("bearish", True)])
def test_full_research_can_veto_the_call(session, scanned, stance, llm):
    assert emerging_recommendation(session, _report(session, "BRKO", stance, llm=llm)) is None


def test_ai_confirmed_call_is_stamped_with_the_model(session, scanned):
    rec = emerging_recommendation(session, _report(session, "BRKO", "bullish", llm=True))
    assert rec.engine_version == "emerging-v1+llm:test-model"
    assert "AI (test-model): AI thesis here." in rec.rationale


def test_weak_lens_score_gets_no_call(session, scanned):
    assert emerging_recommendation(session, _report(session, "LAG0", "bullish")) is None


def test_short_horizon_calls_expire_sooner(session, scanned):
    rec = emerging_recommendation(session, _report(session, "BRKO", "neutral"))
    rec.generated_at = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    rec.stop_price, rec.target_price = D("0.01"), D("100000")  # never touched
    session.flush()
    update_statuses(session, today=dt.date(2026, 4, 1))  # 90 days: still open
    assert rec.status == "open"
    update_statuses(session, today=dt.date(2026, 5, 15))  # 134 days: stale
    assert rec.status == "expired"


def test_scoreboard_can_be_split_by_lens(session):
    from app.db.models import RecommendationOutcome

    t = ensure_ticker(session, "AAA")
    for version, alpha in (("emerging-v1", D("12")), ("rules-v1", D("-3"))):
        rec = Recommendation(ticker_id=t.id, direction="long", horizon="short",
                             generated_at=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
                             entry_price_at_call=D("10"), stop_price=D("9"), target_price=D("12"),
                             status="closed", confidence=D("60"), rationale="x",
                             evidence_json=[], engine_version=version)
        session.add(rec)
        session.flush()
        session.add(RecommendationOutcome(recommendation_id=rec.id, alpha_30d=alpha,
                                          outcome_pct_30d=alpha))
    session.flush()
    assert scoreboard(session, lens="emerging").overall[30]["avg_alpha_pct"] == D("12.00")
    assert scoreboard(session, lens="core").overall[30]["avg_alpha_pct"] == D("-3.00")
    assert scoreboard(session).overall[30]["graded"] == 2


def test_ai_dossier_carries_the_lens_signals(session, scanned):
    from app.research.llm_advisor import SYSTEM_PROMPT, build_dossier

    dossier = build_dossier(session, _report(session, "BRKO", "neutral"))
    assert dossier["emerging_signals"]["lens_rank"] == 1
    assert dossier["emerging_signals"]["breakout"] is True
    assert build_dossier(session, _report(session, "DOWN", "neutral"))["emerging_signals"] is None
    assert "emerging_signals" in SYSTEM_PROMPT
