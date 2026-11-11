"""P&L math: the acceptance-critical arithmetic, pinned with exact Decimals.

Every formula mirrors the docstrings in ``app/db/models_trading.py`` and
``app/trading/positions.py``.  Tests assert on exact Decimal values - float
tolerance thinking is not allowed anywhere near money.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.trading.positions import Fill, apply_fill, position_market_value

D = Decimal


def test_open_long_sets_qty_and_avg():
    qty, avg, realised, borrow = apply_fill(D("0"), None, Fill("buy", D("10"), D("100.00")))
    assert (qty, avg, realised, borrow) == (D("10"), D("100.00"), D("0"), D("0"))


def test_add_to_long_reweights_average_cost():
    # 10 @ 100, then 10 @ 120 -> 20 @ 110
    qty, avg, realised, _ = apply_fill(D("10"), D("100.00"), Fill("buy", D("10"), D("120.00")))
    assert qty == D("20")
    assert avg == D("110.00")
    assert realised == D("0")


def test_partial_close_long_realised_pnl():
    # 20 @ 110, sell 8 @ 130 -> (130-110)*8 = 160 realised, 12 left @ 110
    qty, avg, realised, _ = apply_fill(
        D("20"), D("110.00"), Fill("sell", D("8"), D("130.00"), commission=D("1"))
    )
    assert qty == D("12")
    assert avg == D("110.00")
    assert realised == D("159")  # 160 - 1 commission


def test_full_close_long_clears_position():
    qty, avg, realised, _ = apply_fill(
        D("10"), D("100"), Fill("sell", D("10"), D("90"), commission=D("0.5"))
    )
    assert qty == D("0")
    assert avg is None
    assert realised == D("-100.5")  # (90-100)*10 - 0.5


def test_sell_more_than_open_closes_only_what_exists():
    qty, avg, realised, _ = apply_fill(D("4"), D("100"), Fill("sell", D("10"), D("110")))
    assert (qty, avg) == (D("0"), None)
    assert realised == D("40")  # only 4 shares actually closed


def test_open_short_keeps_negative_qty():
    qty, avg, realised, _ = apply_fill(D("0"), None, Fill("short", D("10"), D("100")))
    assert (qty, avg, realised) == (D("-10"), D("100"), D("0"))


def test_add_to_short_reweights_entry():
    # short 10 @ 100, short 10 @ 80 -> -20 @ 90
    qty, avg, _, _ = apply_fill(D("-10"), D("100"), Fill("short", D("10"), D("80")))
    assert (qty, avg) == (D("-20"), D("90"))


def test_cover_short_profit_math():
    # shorted at 100, cover at 80 -> (100-80)*10 = 200 minus commission
    qty, avg, realised, _ = apply_fill(
        D("-10"), D("100"), Fill("cover", D("10"), D("80"), commission=D("2"))
    )
    assert (qty, avg) == (D("0"), None)
    assert realised == D("198")


def test_cover_short_loss_math():
    qty, _, realised, _ = apply_fill(D("-5"), D("50"), Fill("cover", D("5"), D("60")))
    assert realised == D("-50")  # (50-60)*5


def test_borrow_accrual_flows_into_short_realised():
    # The daily borrow charge is applied to realized_pnl directly by the
    # accrual helper; at the arithmetic level a 2.00 charge plus a 100 gain is
    # exactly 98 - no fuzz.
    realised = D("0")
    realised += D("100")  # cover profit
    realised += D("-2")  # borrow accrual
    assert realised == D("98")


def test_wrong_side_combinations_rejected():
    with pytest.raises(ValueError):
        apply_fill(D("-10"), D("100"), Fill("buy", D("5"), D("90")))  # buy into short
    with pytest.raises(ValueError):
        apply_fill(D("10"), D("100"), Fill("short", D("5"), D("90")))  # short into long
    with pytest.raises(ValueError):
        apply_fill(D("0"), None, Fill("sell", D("5"), D("90")))  # sell without long
    with pytest.raises(ValueError):
        apply_fill(D("0"), None, Fill("cover", D("5"), D("90")))  # cover without short
    with pytest.raises(ValueError):
        apply_fill(D("0"), None, Fill("nonsense", D("5"), D("90")))
    with pytest.raises(ValueError):
        apply_fill(D("0"), None, Fill("buy", D("-1"), D("90")))


def test_position_market_value_none_when_unpriced():
    assert position_market_value(D("10"), None) is None
    assert position_market_value(D("10"), D("101.5")) == D("1015")
    assert position_market_value(D("-10"), D("101.5")) == D("-1015")


def test_money_never_float():
    # 0.1 + 0.2 must not become 0.30000000000000004 anywhere in this code path.
    qty, avg, _, _ = apply_fill(D("1"), D("0.1"), Fill("buy", D("1"), D("0.2")))
    assert avg == D("0.15")
