"""Walk-forward lens backtest: grouping, outcome grading, no look-ahead.

A synthetic market with known futures: a steady leader that keeps rising,
laggards that go nowhere, and a flat benchmark.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal as D

from app.db.models import AppSetting, PriceHistory
from app.ingestion.ingest import ensure_ticker
from app.research.backtest import (
    CAVEATS, load_result, month_ends, run_backtest, save_result,
)
from tests.conftest import FakeProvider, make_bar

START = dt.date(2024, 1, 1)
DAYS = 700


def _series(session, symbol: str, price_of, *, index: bool = False) -> None:
    t = ensure_ticker(session, symbol, in_universe=not index,
                      asset_class="index" if index else "stock")
    for i in range(DAYS):
        p = price_of(i)
        session.add(PriceHistory(ticker_id=t.id, date=START + dt.timedelta(days=i),
                                 close=p, adj_close=p, high=p * D("1.01"), low=p * D("0.99"),
                                 volume=1_000_000, source="fake"))
    session.flush()


def _market(session) -> None:
    _series(session, "^GSPC", lambda i: D("4000"), index=True)
    _series(session, "LEAD", lambda i: D("20") * (D("1.004") ** i))  # +0.4%/day, forever
    for k in range(8):
        _series(session, f"LAG{k}", lambda i, k=k: D("50") + (D("0.5") if i % 2 else D("0"))
                + D(k) / 10)


def test_month_ends_need_room_to_grade():
    dates = [START + dt.timedelta(days=i) for i in range(100)]
    ends = month_ends(dates, months=12, min_forward=21)
    assert ends == [dt.date(2024, 1, 31), dt.date(2024, 2, 29)]  # March has no 21 days after
    assert month_ends(dates, months=1, min_forward=21) == [dt.date(2024, 2, 29)]


def test_leader_tops_every_month_and_beats_the_baseline(session):
    _market(session)
    result = run_backtest(session, benchmark_symbol="^GSPC", months=12, top_n=1)
    assert result["months"] == 12 and result["caveats"] == list(CAVEATS)
    assert all(month["top"][0]["symbol"] == "LEAD" for month in result["by_date"])
    top, everyone = result["groups"]["top"], result["groups"]["all"]
    assert top["n"] == 12 and everyone["n"] == 12 * 9
    assert D(top["avg_excess_21d"]) > D(everyone["avg_excess_21d"])
    assert D(top["avg_return_21d"]) > D("8")       # 1.004^21 - 1 ~ 8.7 %
    assert top["beat_benchmark_21d_pct"] == "100.0"
    assert D(top["target_first_pct"]) > D("0")     # +25 % within 63 bars at 0.4 %/day
    assert top["stop_first_pct"] == "0.0"


def test_no_look_ahead(session):
    """Rewriting everything after the first month-end must not change that
    month-end's scores - only its forward outcomes."""
    _market(session)
    before = run_backtest(session, benchmark_symbol="^GSPC", months=12, top_n=9)
    first = dt.date.fromisoformat(before["by_date"][0]["date"])
    lead = ensure_ticker(session, "LEAD")
    for bar in session.query(PriceHistory).filter(PriceHistory.ticker_id == lead.id,
                                                  PriceHistory.date > first):
        bar.close = bar.adj_close = D("1")
        bar.high = bar.low = D("1")
    session.flush()
    after = run_backtest(session, benchmark_symbol="^GSPC", months=12, top_n=9)

    def scores(result):
        return {p["symbol"]: p["score"] for p in result["by_date"][0]["top"]}

    def ret(result):
        return next(p for p in result["by_date"][0]["top"] if p["symbol"] == "LEAD")["ret21"]

    assert scores(before) == scores(after)
    assert D(ret(after)) < D("-90") < D(ret(before))


def test_missing_benchmark_is_an_error_not_a_crash(session):
    assert "error" in run_backtest(session, benchmark_symbol="^NOPE")


def test_result_round_trips_through_settings(session):
    assert load_result(session) is None
    save_result(session, {"months": 3, "groups": {}})
    session.flush()
    assert load_result(session) == {"months": 3, "groups": {}}
    session.query(AppSetting).filter_by(key="lens_backtest").one().value = "{broken"
    assert load_result(session) is None


def test_history_extension_runs_once(session):
    from app.config import Settings
    from app.jobs import extend_history

    today = dt.date(2026, 11, 11)
    t = ensure_ticker(session, "ACME", in_universe=True, asset_class="stock")
    session.add(PriceHistory(ticker_id=t.id, date=today, close=D("10"), source="fake"))
    session.flush()
    old = [make_bar("ACME", today - dt.timedelta(days=900 + i), "9") for i in range(3)]
    provider = FakeProvider(bars=old)
    settings = Settings(benchmark_symbol="^GSPC")
    assert extend_history(session, provider, settings, today) == 1
    assert session.query(PriceHistory).filter_by(ticker_id=t.id).count() == 4
    assert extend_history(session, provider, settings, today) == 0  # stamped
