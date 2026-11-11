"""Ranked ideas list: confidence ordering, newest-per-ticker dedupe, and
the held-in-real/paper tagging that makes the list actionable."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal as D

import pytest

from app.config import Settings
from app.db.models import Position, Recommendation
from app.ingestion.ingest import ensure_ticker
from app.research.ranking import ranked_ideas
from app.tracking import record_trade
from app.trading.accounts import get_or_open_account

BASE = dt.datetime(2026, 1, 5, 12, tzinfo=dt.timezone.utc)


def _rec(session, symbol: str, confidence: str, *, direction: str = "long",
         age_days: int = 0) -> Recommendation:
    rec = Recommendation(
        ticker_id=ensure_ticker(session, symbol).id, direction=direction,
        entry_price_at_call=D("100"), stop_price=D("90"),
        confidence=D(confidence), rationale="test",
        generated_at=BASE - dt.timedelta(days=age_days),
    )
    session.add(rec)
    session.flush()
    return rec


def test_ranked_by_confidence_desc(session):
    _rec(session, "MID", "50")
    _rec(session, "BEST", "90")
    _rec(session, "OK", "70")
    ideas = ranked_ideas(session)
    assert [(i.rank, i.symbol) for i in ideas] == [(1, "BEST"), (2, "OK"), (3, "MID")]


def test_newest_call_per_ticker_wins_even_if_lower_confidence(session):
    _rec(session, "DUP", "90", age_days=10)   # older, juicier, but stale
    _rec(session, "DUP", "70", age_days=1)    # newest open call
    _rec(session, "OTHER", "65")
    ideas = ranked_ideas(session)
    assert [i.symbol for i in ideas] == ["DUP", "OTHER"]
    assert ideas[0].rec.confidence == D("70")


def test_held_tags_real_beats_paper(session):
    account = get_or_open_account(session)
    _rec(session, "PAPER", "80")
    _rec(session, "REAL", "85")
    _rec(session, "BOTH", "88")
    _rec(session, "FRESH", "90")

    for symbol in ("PAPER", "BOTH"):
        session.add(Position(account_id=account.id,
                             ticker_id=ensure_ticker(session, symbol).id,
                             qty=D("5"), avg_cost=D("100")))
    record_trade(session, "REAL", "buy", D("1"), D("100"))
    record_trade(session, "BOTH", "buy", D("2"), D("100"))
    session.flush()

    held = {i.symbol: i.held for i in ranked_ideas(session)}
    assert held == {"FRESH": None, "BOTH": "real", "REAL": "real", "PAPER": "paper"}


def test_sell_down_to_flat_clears_held_tag(session):
    _rec(session, "FLAT", "80")
    record_trade(session, "FLAT", "buy", D("3"), D("100"))
    record_trade(session, "FLAT", "sell", D("3"), D("110"))
    assert ranked_ideas(session)[0].held is None


def test_limit_and_direction_filter(session):
    _rec(session, "L1", "90")
    _rec(session, "L2", "80")
    _rec(session, "S1", "95", direction="short")
    longs = ranked_ideas(session, direction="long", limit=1)
    assert [i.symbol for i in longs] == ["L1"]


def test_suggested_qty_uses_risk_sizing(session):
    settings = Settings()
    account = get_or_open_account(session, settings)
    _rec(session, "SIZE", "80")
    (idea,) = ranked_ideas(session, account=account, settings=settings)
    # budget = total_value * risk_pct, qty = floor(budget / (100-90)): exact
    # relationship checked via a second sizing call in app.trading.sizing.
    assert idea.suggested_qty and idea.suggested_qty > 0


def test_last_price_attached_from_stored_bars(session):
    """Entry @ call vs Last: the newest stored close, or None (never a guess)."""
    from app.db.models import PriceHistory

    t = ensure_ticker(session, "PX")
    session.add(PriceHistory(ticker_id=t.id, date=dt.date(2026, 1, 2),
                             close=D("101.5"), source="fake"))
    session.flush()
    _rec(session, "PX", "80")
    _rec(session, "BARE", "70")
    ideas = {i.symbol: i for i in ranked_ideas(session)}
    assert ideas["PX"].last_price == D("101.5")
    assert ideas["PX"].last_price_date == dt.date(2026, 1, 2)
    assert ideas["BARE"].last_price is None
