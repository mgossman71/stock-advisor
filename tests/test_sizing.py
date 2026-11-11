"""Risk-per-trade sizing helper."""

from __future__ import annotations

from decimal import Decimal as D

from app.config import Settings
from app.trading.sizing import suggested_size

SETTINGS = Settings(risk_per_trade_pct=D("1"), initial_cash=D("100000"))


def test_basic_long_size():
    # 1 % of 100k = 1000 budget; 50-48 = 2 per share -> 500 shares.
    assert suggested_size(D("100000"), D("50"), D("48"), settings=SETTINGS) == 500


def test_short_uses_stop_above_entry():
    assert suggested_size(D("100000"), D("50"), D("52"), settings=SETTINGS) == 500


def test_rounds_down_to_whole_shares():
    # budget 1000 / 3 per share = 333.33 -> 333
    assert suggested_size(D("100000"), D("51"), D("48"), settings=SETTINGS) == 333


def test_risk_override_pct():
    assert suggested_size(D("100000"), D("50"), D("48"), risk_pct=D("2"),
                         settings=SETTINGS) == 1000


def test_none_when_stop_equals_entry_or_inputs_missing():
    assert suggested_size(D("100000"), D("50"), D("50"), settings=SETTINGS) is None
    assert suggested_size(None, D("50"), D("48"), settings=SETTINGS) is None
    assert suggested_size(D("100000"), None, D("48"), settings=SETTINGS) is None
    # A size below one whole share is "no trade", not a fractional share:
    # 1 % of 50 is a 0.50 budget, less than the 1-per-share risk.
    assert suggested_size(D("50"), D("99"), D("98"), settings=SETTINGS) is None
