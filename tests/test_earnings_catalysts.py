"""Market-wide earnings calendar -> earnings-drift signal; catalysts; micro caps.

No network: Nasdaq parsing runs against a canned payload, everything else
against ``FakeProvider(calendar=...)``.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal as D

from app.config import get_settings
from app.db.models import AnalystAction, DataFetchLog, EarningsEvent, NewsItem, ScanScore
from app.ingestion.ingest import (
    CALENDAR_AHEAD_DAYS, earnings_calendar_days, ensure_ticker, ingest_earnings_calendar,
)
from app.providers.base import EarningsEvent as Payload
from app.providers.fallback import FallbackProvider
from app.research import emerging
from app.research.emerging import Bars, Earnings
from app.research.scan import confirm_emerging, emerging_priority, run_scan
from app.research.screener import emerging_recommendation
from tests.conftest import FakeProvider
from tests.test_emerging import TODAY, _report, as_bars, breakout, laggard, store

FRI = dt.date(2026, 11, 6)


# ----------------------------------------------------------------- provider

def test_nasdaq_calendar_parsing(monkeypatch):
    from app.providers.nasdaq_provider import NasdaqProvider

    payload = {"rows": [
        {"symbol": "MRNA", "eps": "$0.38", "epsForecast": "($1.10)", "surprise": "134.55"},
        {"symbol": "BRK/B", "eps": "N/A", "epsForecast": "$4.10", "surprise": "N/A"},
        {"symbol": "", "eps": "$1"},
    ]}
    provider = NasdaqProvider(get_settings())
    monkeypatch.setattr(provider, "_get_json", lambda path, params: payload)
    result = provider.get_earnings_calendar(FRI)
    assert result.ok and result.row_count == 2
    mrna, brk = result.data
    assert (mrna.eps_estimate, mrna.eps_actual, mrna.surprise_pct) == (
        D("-1.10"), D("0.38"), D("134.55"))
    assert mrna.status == "reported" and mrna.event_date == FRI
    assert brk.symbol == "BRK-B" and brk.status == "scheduled"
    assert brk.eps_actual is None and brk.surprise_pct is None  # unknown, never 0


def test_fallback_accepts_an_empty_day_and_skips_unsupported():
    chain = FallbackProvider([FakeProvider(name="old"), FakeProvider(calendar={})])
    result = chain.get_earnings_calendar(dt.date(2026, 11, 8))
    assert result.ok and result.row_count == 0  # nobody reported: still success


# ---------------------------------------------------------------- ingestion

def _beat(symbol: str, day: dt.date, surprise: str = "40") -> Payload:
    return Payload(symbol=symbol, event_date=day, eps_estimate=D("1.00"),
                   eps_actual=D("1.40"), surprise_pct=D(surprise), status="reported",
                   source="fake")


def test_calendar_only_touches_tracked_tickers_and_never_unreports(session):
    ensure_ticker(session, "ACME")
    provider = FakeProvider(calendar={FRI: [_beat("ACME", FRI), _beat("NOPE", FRI)]})
    stats = ingest_earnings_calendar(session, provider, FRI)
    assert (stats.fetched, stats.written, stats.skipped) == (2, 1, 1)
    assert session.query(EarningsEvent).count() == 1  # NOPE was not created
    provider.calendar = {FRI: [Payload(symbol="ACME", event_date=FRI, status="scheduled",
                                       source="fake")]}
    ingest_earnings_calendar(session, provider, FRI)
    row = session.query(EarningsEvent).one()
    assert row.status == "reported" and row.surprise_pct == D("40")


def test_only_unsettled_days_are_fetched(session):
    today = dt.date(2026, 11, 11)
    first = earnings_calendar_days(session, today)
    assert all(d.weekday() < 5 for d in first)
    assert first[-1] <= today + dt.timedelta(days=CALENDAR_AHEAD_DAYS)
    old, recent = dt.date(2026, 11, 2), dt.date(2026, 11, 10)
    for day in (old, recent):  # both fetched "today": only the old one is settled
        session.add(DataFetchLog(provider="nasdaq", endpoint=f"earnings_calendar({day})",
                                 status="ok", fetched_at=dt.datetime(2026, 11, 11, 3,
                                                                     tzinfo=dt.timezone.utc)))
    session.flush()
    second = earnings_calendar_days(session, today)
    assert old not in second and recent in second
    assert len(second) == len(first) - 1


# -------------------------------------------------------------- the signal

def _dated(shape, end: dt.date) -> Bars:
    bars = as_bars(shape)
    bars.dates = [end - dt.timedelta(days=len(shape) - 1 - i) for i in range(len(shape))]
    return bars


def test_beat_that_holds_scores_and_one_that_fades_does_not():
    bars = _dated(breakout(), TODAY)
    reported = TODAY - dt.timedelta(days=6)  # before the last 4-day breakout leg
    held = emerging.signals(bars, earnings=Earnings(reported_on=reported,
                                                    surprise_pct=D("30")))
    points_held, reasons = emerging.score(held, D("90"))
    assert any("beat EPS estimates by 30% 6 days ago and held the gains" in r for r in reasons)
    faded_bars = _dated(breakout()[:-4] + [(D("70"), 1_000_000)] * 4, TODAY)
    faded = emerging.signals(faded_bars, earnings=Earnings(reported_on=reported,
                                                           surprise_pct=D("30")))
    points_faded, faded_reasons = emerging.score(faded, D("90"))
    assert any("gave the gains back" in r for r in faded_reasons)
    plain, _ = emerging.score(emerging.signals(bars), D("90"))
    assert points_held - plain == D("10")


def test_misses_cost_points_and_stale_or_upcoming_reports_are_handled():
    bars = _dated(laggard(), TODAY)
    base, _ = emerging.score(emerging.signals(bars), D("50"))
    miss, reasons = emerging.score(emerging.signals(bars, earnings=Earnings(
        reported_on=TODAY - dt.timedelta(days=3), surprise_pct=D("-25"))), D("50"))
    assert base - miss == D("5") and any("missed EPS estimates by 25%" in r for r in reasons)
    stale, _ = emerging.score(emerging.signals(bars, earnings=Earnings(
        reported_on=TODAY - dt.timedelta(days=60), surprise_pct=D("-25"))), D("50"))
    assert stale == base  # older than 30 days: ignored
    _, soon = emerging.score(emerging.signals(bars, earnings=Earnings(
        next_on=TODAY + dt.timedelta(days=4))), D("50"))
    assert "note: earnings due in 4 days (event risk)" in soon


def test_scan_feeds_stored_earnings_to_the_lens(session):
    ticker = store(session, "BRKO", breakout())
    for i in range(6):
        store(session, f"LAG{i}", laggard(slope=f"0.0{i}"))
    session.add(EarningsEvent(ticker_id=ticker.id, event_date=TODAY - dt.timedelta(days=6),
                              eps_actual=D("1.4"), eps_estimate=D("1"), surprise_pct=D("40"),
                              status="reported", source="fake"))
    session.flush()
    run_scan(session, today=TODAY)
    row = session.query(ScanScore).filter_by(ticker_id=ticker.id).one()
    assert row.emerging["eps_surprise_pct"] == "40.00"
    assert row.emerging["held_post_earnings_gains"] is True


# --------------------------------------------------------------- catalysts

def test_catalyst_points():
    assert emerging.catalyst_points(0, 0, None) == (D("0"), [])
    up, reasons = emerging.catalyst_points(4, 0, D("30"))
    assert up == D("16") and len(reasons) == 2  # +12 cap, +4 news
    down, _ = emerging.catalyst_points(0, 1, D("-20"))
    assert down == D("-8")


def test_confirm_reorders_candidates_by_catalysts(session):
    store(session, "BRKO", breakout())
    second = store(session, "BRK2", breakout()[:-1] + [(D("85"), 3_000_000)])
    for i in range(6):
        store(session, f"LAG{i}", laggard(slope=f"0.0{i}"))
    run_scan(session, today=TODAY)
    before = emerging_priority(session)
    assert before.index("BRKO") < before.index("BRK2")
    for _ in range(3):
        session.add(AnalystAction(ticker_id=second.id, action_date=TODAY, action="upgrade",
                                  source="fake"))
    session.add(NewsItem(ticker_id=second.id, title="Big win", url="https://x",
                         published_at=dt.datetime.combine(TODAY, dt.time(),
                                                          tzinfo=dt.timezone.utc),
                         sentiment_score=D("40"), provider="fake"))
    session.flush()
    assert confirm_emerging(session, TODAY) >= 2
    after = emerging_priority(session)
    assert after.index("BRK2") < after.index("BRKO")
    row = session.query(ScanScore).filter_by(ticker_id=second.id).one()
    assert row.emerging["catalysts"]["points"] == "16"


# --------------------------------------------------------------- micro caps

def test_micro_caps_are_scored_flagged_and_capped(session):
    sig = emerging.signals(as_bars(laggard()), market_cap=D("1.5e8"))
    _, reasons = emerging.score(sig, D("50"))
    assert "micro cap: high risk, thin trading" in reasons
    ticker = store(session, "TINY", breakout())
    ticker.market_cap = D("1.2e8")
    for i in range(6):
        store(session, f"LAG{i}", laggard(slope=f"0.0{i}"))
    run_scan(session, today=TODAY)
    rec = emerging_recommendation(session, _report(session, "TINY", "neutral"))
    assert rec.rationale.startswith("MICRO CAP — high risk. ")
    assert rec.confidence <= emerging.MICRO_CAP_MAX_CONFIDENCE
