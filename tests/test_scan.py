"""Discovery funnel: the cheap scan, the shortlist, and what gets the full fetch.

Pins: names with fundamentals rank ahead of price-only names; too little
history is never scored; a re-scan the same day replaces rather than
duplicates; the shortlist (not the universe) is what the deep screen and the
LLM see; the fundamentals rotation goes never-fetched first, then stalest;
changing a floor makes the stock list due for a rebuild.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import replace
from decimal import Decimal as D

from app.config import Settings
from app.db.models import Fundamentals, PriceHistory, ScanScore
from app.ingestion.ingest import ensure_ticker
from app.research import scan
from app.research.scan import (
    config_from_form, fundamentals_due, load_scan_config, record_universe_refresh,
    run_scan, save_scan_config, shortlist_symbols, universe_due,
)
from app.research.screener import screening_candidates

DAY = dt.date(2026, 1, 5)


def _bars(session, symbol: str, n: int, *, start: str = "100", step: str = "1",
          **ticker_kwargs):
    ticker = ensure_ticker(session, symbol, in_universe=True, **ticker_kwargs)
    for i in range(n):
        close = D(start) + D(step) * i
        session.add(PriceHistory(ticker_id=ticker.id, date=DAY - dt.timedelta(days=n - i),
                                 close=close, adj_close=close, source="fake"))
    session.flush()
    return ticker


def _fund(session, ticker, *, as_of: dt.date = DAY, **values):
    defaults = {"pe_trailing": D("15"), "return_on_equity": D("20"),
                "profit_margin": D("15"), "debt_to_equity": D("0.5")}
    defaults.update(values)
    session.add(Fundamentals(ticker_id=ticker.id, as_of_date=as_of, source="fake", **defaults))
    session.flush()


def test_fundamentals_rank_ahead_of_price_only_and_short_history_is_skipped(session):
    rocket = _bars(session, "ROCKET", 120, step="2")     # great momentum, no fundamentals
    steady = _bars(session, "STEADY", 120, step="0.1")
    _fund(session, steady)
    _bars(session, "NEWIPO", 20)                         # too little history

    result = run_scan(session, today=DAY, shortlist_size=1)

    assert result.universe == 3 and result.scored == 2
    assert result.skipped_short_history == 1
    assert result.with_fundamentals == 1
    assert result.shortlist == ["STEADY"], "scored on the whole picture beats price-only"
    rows = {r.ticker.symbol: r for r in session.query(ScanScore)}
    assert rows["STEADY"].rank == 1 and rows["STEADY"].has_fundamentals
    assert rows["ROCKET"].rank == 2 and not rows["ROCKET"].has_fundamentals
    assert rows["ROCKET"].sub_scores["value"] is None, "unknown stays unknown, never 0"
    assert "NEWIPO" not in rows
    assert rocket.id == rows["ROCKET"].ticker_id


def test_rescan_same_day_replaces_rows_and_old_scans_are_pruned(session):
    _bars(session, "ACME", 80)
    run_scan(session, today=DAY - dt.timedelta(days=30))
    run_scan(session, today=DAY)
    run_scan(session, today=DAY)
    assert session.query(ScanScore).count() == 1
    assert session.query(ScanScore).one().scan_date == DAY


def test_shortlist_is_the_top_n_of_the_latest_scan(session):
    for i, symbol in enumerate(("AAA", "BBB", "CCC")):
        _fund(session, _bars(session, symbol, 80, step=str(i + 1)))
    assert shortlist_symbols(session) == [], "no scan yet"
    run_scan(session, today=DAY)
    assert len(shortlist_symbols(session, 2)) == 2
    assert len(shortlist_symbols(session, 10)) == 3


def test_deep_screen_sees_shortlist_and_holdings_not_the_universe(session):
    from app.tracking import record_trade

    for symbol in ("AAA", "BBB", "CCC", "DDD"):
        _fund(session, _bars(session, symbol, 80))
    ensure_ticker(session, "WATCHED", in_watchlist=True)
    record_trade(session, "OWNED", "buy", D("1"), D("10"), trade_date=DAY)
    run_scan(session, today=DAY, shortlist_size=1)
    save_scan_config(session, replace(load_scan_config(session), shortlist_size=1))

    symbols = [t.symbol for t in screening_candidates(session)]
    shortlisted = shortlist_symbols(session)
    assert len(shortlisted) == 1
    assert sorted(symbols) == sorted(shortlisted + ["OWNED", "WATCHED"])


def test_fundamentals_rotation_is_never_fetched_first_then_stalest(session):
    fresh = _bars(session, "FRESH", 5)
    _fund(session, fresh, as_of=DAY - dt.timedelta(days=1))
    stale = _bars(session, "STALE", 5)
    _fund(session, stale, as_of=DAY - dt.timedelta(days=30))
    older = _bars(session, "OLDER", 5)
    _fund(session, older, as_of=DAY - dt.timedelta(days=60))
    _bars(session, "NEVER", 5)

    assert fundamentals_due(session, 10, DAY) == ["NEVER", "OLDER", "STALE"]
    assert fundamentals_due(session, 2, DAY) == ["NEVER", "OLDER"]
    assert fundamentals_due(session, 0, DAY) == []


def test_config_defaults_from_env_and_form_edits_in_millions(session):
    settings = Settings(scan_min_market_cap=D("300000000"), shortlist_size=25)
    config = load_scan_config(session, settings)
    assert config.min_market_cap == D("300000000") and config.shortlist_size == 25

    edited = config_from_form({"scan_min_market_cap_m": "1,000", "scan_min_price": "",
                               "scan_min_dollar_volume_m": "2.5",
                               "scan_shortlist_size": "9999",
                               "scan_fundamentals_per_night": "junk"}, config)
    assert edited.min_market_cap == D("1000000000")
    assert edited.min_price == config.min_price, "blank keeps the current value"
    assert edited.min_dollar_volume == D("2500000")
    assert edited.shortlist_size == 500, "clamped"
    assert edited.fundamentals_per_night == config.fundamentals_per_night


def test_universe_refresh_is_weekly_and_a_floor_change_forces_it(session):
    now = dt.datetime(2026, 1, 5, tzinfo=dt.timezone.utc)
    settings = Settings(universe_refresh_days=7)
    assert universe_due(session, settings, now)
    record_universe_refresh(session, "nasdaq", 2999, now=now)
    assert not universe_due(session, settings, now + dt.timedelta(days=6))
    assert universe_due(session, settings, now + dt.timedelta(days=7))

    config = load_scan_config(session, settings)
    assert not save_scan_config(session, replace(config, shortlist_size=40)), \
        "shortlist size is not a floor"
    assert not universe_due(session, settings, now)
    assert save_scan_config(session, replace(config, min_price=D("10")))
    assert universe_due(session, settings, now)


def test_fallback_refresh_records_source_but_not_the_stamp(session):
    record_universe_refresh(session, "csv", 503, stamp=False)
    info = scan.universe_info(session)
    assert info.source == "csv" and info.count == 503 and info.refreshed_at is None
    assert universe_due(session)
