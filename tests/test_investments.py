"""Real-money ledger: FIFO cost basis, holding-period classification and
tax-liability estimates.  All offline: bars are inserted by hand."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal as D

import pytest

from app.db.models import AppSetting, PriceHistory, Recommendation
from app.ingestion.ingest import ensure_ticker
from app.tracking import (
    RealTradeError,
    get_tax_rates,
    lot_valuations,
    open_lots,
    record_trade,
    realized_matches,
    set_tax_rates,
    tax_summary,
)
from app.tracking.taxes import TaxRateError, holding_class

ASOF = dt.date(2026, 11, 10)


def _bar(session, symbol: str, date: dt.date, close: str) -> None:
    session.add(PriceHistory(ticker_id=ensure_ticker(session, symbol).id,
                             date=date, close=D(close), source="fake"))
    session.flush()


def test_fifo_partial_matches_across_lots(session):
    _bar(session, "FIFO", dt.date(2025, 1, 2), "100")
    record_trade(session, "FIFO", "buy", D("10"), D("100"),
                 trade_date=dt.date(2025, 1, 2))
    record_trade(session, "FIFO", "buy", D("5"), D("120"),
                 trade_date=dt.date(2025, 6, 1))
    record_trade(session, "FIFO", "sell", D("12"), D("150"),
                 trade_date=dt.date(2026, 2, 1))

    matches = realized_matches(session)
    assert [(m.qty, m.gross_gain) for m in matches] == [
        (D("10"), D("500")),  # the 100-cost lot is consumed first
        (D("2"), D("60")),
    ]
    lots = open_lots(session, "FIFO")
    assert len(lots) == 1 and lots[0].remaining == D("3")
    assert lots[0].trade.price == D("120")


def test_commissions_land_in_basis_and_proceeds(session):
    record_trade(session, "COMM", "buy", D("10"), D("100"),
                 trade_date=dt.date(2026, 1, 5), commission=D("5"))
    record_trade(session, "COMM", "sell", D("10"), D("110"),
                 trade_date=dt.date(2026, 3, 5), commission=D("5"))
    (m,) = realized_matches(session)
    assert m.cost_per_share == D("100.5")     # 1000 + 5 commission / 10
    assert m.proceeds_per_share == D("109.5")  # 1100 - 5 commission / 10
    assert m.gross_gain == D("90")


def test_sells_cannot_exceed_holdings_and_garbage_rejected(session):
    record_trade(session, "HOLD", "buy", D("10"), D("50"),
                 trade_date=dt.date(2026, 1, 5))
    with pytest.raises(RealTradeError):
        record_trade(session, "HOLD", "sell", D("10.0001"), D("60"),
                     trade_date=dt.date(2026, 2, 5))
    with pytest.raises(RealTradeError):
        record_trade(session, "NEW", "sell", D("1"), D("10"),
                     trade_date=dt.date(2026, 2, 5))  # never bought
    with pytest.raises(RealTradeError):
        record_trade(session, "HOLD", "buy", D("0"), D("50"))
    with pytest.raises(RealTradeError):
        record_trade(session, "HOLD", "buy", D("1"), D("-3"))
    with pytest.raises(RealTradeError):
        record_trade(session, "HOLD", "whatever", D("1"), D("10"))
    assert len(open_lots(session, "HOLD")) == 1  # nothing else landed


def test_holding_class_boundary():
    assert holding_class(365) == "short"
    assert holding_class(366) == "long"


def test_lot_valuations_classify_and_estimate(session):
    """Default rates 32%/15%: short lot up, long lot down, each per its class."""
    short_entry = ASOF - dt.timedelta(days=40)
    long_entry = ASOF - dt.timedelta(days=430)
    _bar(session, "UP", short_entry, "100")
    _bar(session, "UP", dt.date(2026, 11, 9), "110")
    _bar(session, "DOWN", long_entry, "50")
    _bar(session, "DOWN", dt.date(2026, 11, 9), "45")
    record_trade(session, "UP", "buy", D("10"), D("100"), trade_date=short_entry)
    record_trade(session, "DOWN", "buy", D("1"), D("50"), trade_date=long_entry)

    vals = {v.symbol: v for v in lot_valuations(session, ASOF)}
    assert vals["UP"].klass == "short" and vals["UP"].held_days == 40
    assert vals["UP"].unrealized == D("100")
    assert vals["UP"].est_tax_if_sold == D("32.00")
    assert vals["DOWN"].klass == "long" and vals["DOWN"].held_days == 430
    assert vals["DOWN"].unrealized == D("-5")
    assert vals["DOWN"].est_tax_if_sold == D("0")  # loss: no tax, benefit shown via net

    summary = tax_summary(session, ASOF)
    assert summary["st_gain_unreal"] == D("100")
    assert summary["lt_loss_unreal"] == D("5")
    # 100 * 0.32 - 5 * 0.15 = 31.25  (losses assumed to offset at their own class rate)
    assert summary["est_tax_if_sold_today"] == D("31.25")


def test_realized_buckets_classify_at_sale_date(session):
    record_trade(session, "OLD", "buy", D("10"), D("100"),
                 trade_date=dt.date(2024, 1, 2))
    record_trade(session, "OLD", "sell", D("10"), D("150"),
                 trade_date=dt.date(2026, 1, 2))   # held 731d -> long-term
    record_trade(session, "NEW", "buy", D("1"), D("50"),
                 trade_date=dt.date(2026, 5, 1))
    record_trade(session, "NEW", "sell", D("1"), D("40"),
                 trade_date=dt.date(2026, 6, 1))   # held 31d -> short-term loss

    summary = tax_summary(session, ASOF)
    assert summary["lt_gain_real"] == D("500")
    assert summary["st_loss_real"] == D("10")
    # (0 - 10) * 0.32 + 500 * 0.15 = -3.20 + 75.00
    assert summary["est_tax_realized"] == D("71.80")
    assert summary["est_tax_if_sold_today"] == D("0")  # no open lots


def test_rates_persist_and_apply(session):
    assert get_tax_rates(session) == {"short": D("0.32"), "long": D("0.15")}
    set_tax_rates(session, D("0.35"), D("0.20"))
    assert get_tax_rates(session) == {"short": D("0.35"), "long": D("0.20")}
    assert session.query(AppSetting).filter_by(key="tax_rate_short_term").count() == 1

    _bar(session, "RATE", dt.date(2026, 10, 1), "100")
    _bar(session, "RATE", dt.date(2026, 11, 9), "110")
    record_trade(session, "RATE", "buy", D("1"), D("100"), trade_date=dt.date(2026, 10, 1))
    assert tax_summary(session, ASOF)["est_tax_if_sold_today"] == D("3.50")

    with pytest.raises(TaxRateError):
        set_tax_rates(session, D("0.70"), D("0.20"))  # out of plausible range


def test_unmarked_lot_is_surfaced_not_zeroed(session):
    """No stored bar for the ticker: value/return report as unknown, cost stays."""
    record_trade(session, "BARE", "buy", D("4"), D("25"), trade_date=dt.date(2026, 1, 5))
    (v,) = lot_valuations(session, ASOF)
    assert v.price is None and v.unrealized is None and v.est_tax_if_sold is None
    assert v.cost_basis == D("100")
    summary = tax_summary(session, ASOF)
    assert summary["unrealized_unmarked"] == D("100")
    assert summary["est_tax_if_sold_today"] == D("0")


def test_trade_links_to_recommendation(session):
    ticker = ensure_ticker(session, "LINK")
    rec = Recommendation(
        ticker_id=ticker.id, direction="long", entry_price_at_call=D("100"),
        confidence=D("70"), rationale="test call",
    )
    session.add(rec)
    session.flush()
    trade = record_trade(session, "LINK", "buy", D("5"), D("101"),
                         trade_date=dt.date(2026, 10, 1), recommendation_id=rec.id)
    assert trade.recommendation_id == rec.id
    assert trade.recommendation.id == rec.id
