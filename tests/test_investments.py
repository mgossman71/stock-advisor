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
    """Long-term only when sold *after* the one-year anniversary (IRS rule)."""
    bought = dt.date(2025, 1, 15)
    assert holding_class(bought, dt.date(2026, 1, 15)) == "short"  # the anniversary itself
    assert holding_class(bought, dt.date(2026, 1, 16)) == "long"


def test_holding_class_counts_leap_years_by_calendar():
    # 366 days, but not "more than one year" - a day-count rule gets this wrong.
    assert holding_class(dt.date(2023, 3, 1), dt.date(2024, 3, 1)) == "short"
    assert holding_class(dt.date(2023, 3, 1), dt.date(2024, 3, 2)) == "long"
    # Bought Feb 29: the anniversary is Feb 28 of the next year.
    assert holding_class(dt.date(2024, 2, 29), dt.date(2025, 2, 28)) == "short"
    assert holding_class(dt.date(2024, 2, 29), dt.date(2025, 3, 1)) == "long"


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
    # Schedule-D netting: the 5 LT loss absorbs part of the ST gain, so
    # (100 - 5) * 0.32 = 30.40 - not 100*0.32 - 5*0.15.
    assert summary["est_tax_if_sold_today"] == D("30.40")


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
    # The 10 ST loss offsets LT gain at the LT rate: (500 - 10) * 0.15 = 73.50
    assert summary["est_tax_realized"] == D("73.50")
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


def test_backdated_sell_before_the_buy_is_rejected(session):
    """A sell must be covered by shares held *on its date*, not just today."""
    record_trade(session, "BACK", "buy", D("10"), D("100"), trade_date=dt.date(2026, 11, 1))
    with pytest.raises(RealTradeError, match="exceeds the shares held"):
        record_trade(session, "BACK", "sell", D("10"), D("110"),
                     trade_date=dt.date(2026, 10, 1))
    assert len(open_lots(session, "BACK")) == 1  # the ledger still replays
    # A properly-dated sell is still fine.
    record_trade(session, "BACK", "sell", D("4"), D("110"), trade_date=dt.date(2026, 11, 2))
    assert open_lots(session, "BACK")[0].remaining == D("6")


def test_backdated_sell_cannot_steal_shares_a_later_sell_needed(session):
    record_trade(session, "MID", "buy", D("10"), D("100"), trade_date=dt.date(2026, 1, 5))
    record_trade(session, "MID", "sell", D("10"), D("120"), trade_date=dt.date(2026, 3, 5))
    # Today's net is 0, but the real problem is the March sell would be short.
    with pytest.raises(RealTradeError):
        record_trade(session, "MID", "sell", D("5"), D("110"), trade_date=dt.date(2026, 2, 1))
    assert len(realized_matches(session)) == 1


def test_net_loss_across_buckets_owes_nothing_and_never_goes_negative(session):
    record_trade(session, "STL", "buy", D("10"), D("200"), trade_date=dt.date(2026, 6, 1))
    record_trade(session, "STL", "sell", D("10"), D("100"), trade_date=dt.date(2026, 7, 1))
    record_trade(session, "LTG", "buy", D("10"), D("100"), trade_date=dt.date(2024, 1, 2))
    record_trade(session, "LTG", "sell", D("10"), D("200"), trade_date=dt.date(2026, 7, 2))
    summary = tax_summary(session, ASOF)
    # ST -1000 and LT +1000 net to zero: no tax (was -170 under per-bucket rates).
    assert summary["est_tax_realized"] == D("0.00")
    assert summary["net_capital_loss_realized"] == D("0.00")

    record_trade(session, "STL", "buy", D("10"), D("100"), trade_date=dt.date(2026, 8, 1))
    record_trade(session, "STL", "sell", D("10"), D("50"), trade_date=dt.date(2026, 8, 2))
    summary = tax_summary(session, ASOF)
    assert summary["est_tax_realized"] == D("0.00")
    assert summary["net_capital_loss_realized"] == D("500.00")


def test_realized_tax_covers_the_current_tax_year_only(session):
    record_trade(session, "PRIOR", "buy", D("10"), D("100"), trade_date=dt.date(2025, 2, 1))
    record_trade(session, "PRIOR", "sell", D("10"), D("200"), trade_date=dt.date(2025, 6, 1))
    summary = tax_summary(session, ASOF)
    assert summary["tax_year"] == 2026
    assert summary["st_gain_real"] == D("0")  # the 2025 gain was already filed
    assert summary["est_tax_realized"] == D("0.00")


def test_lots_replay_through_a_recorded_split(session):
    """4:1 split: 10 shares @ 400 read as 40 @ 100; the stored row is untouched."""
    from app.db.models import RealTrade, StockSplit

    ticker = ensure_ticker(session, "SPLT")
    buy = record_trade(session, "SPLT", "buy", D("10"), D("400"), trade_date=dt.date(2026, 1, 5))
    session.add(StockSplit(ticker_id=ticker.id, split_date=dt.date(2026, 6, 1),
                           split_from=1, split_to=4, source="fake"))
    session.flush()
    (lot,) = open_lots(session, "SPLT")
    assert lot.remaining == D("40") and lot.cost_per_share == D("100")
    # Post-split you can sell the post-split share count...
    record_trade(session, "SPLT", "sell", D("40"), D("110"), trade_date=dt.date(2026, 7, 1))
    (m,) = realized_matches(session)
    assert m.gross_gain == D("400")
    assert session.get(RealTrade, buy.id).qty == D("10")  # user's record unchanged
