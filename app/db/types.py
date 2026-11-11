"""Shared column types.

Everything monetary or ratio-like is ``NUMERIC`` so SQLAlchemy hands back
:class:`decimal.Decimal` rather than a float - the P&L math is never allowed to
run on binary floats.  All types chosen here exist identically on SQLite and
Postgres so the schema stays portable.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import Numeric

#: Cash amounts (account balances, realised P&L, commissions).
MONEY = Numeric(20, 4, asdecimal=True)
#: Per-share prices.
PRICE = Numeric(20, 6, asdecimal=True)
#: Signed share counts (shorts are negative).
QTY = Numeric(20, 6, asdecimal=True)
#: Percentages expressed in percent points (12.5 == 12.5 %).
PCT = Numeric(12, 4, asdecimal=True)
#: Unitless ratios (P/E, debt/equity, RSI, composite scores).
RATIO = Numeric(18, 6, asdecimal=True)
#: Market capitalisation and other very large magnitudes.
BIG_AMOUNT = Numeric(24, 0, asdecimal=True)

__all__ = ["MONEY", "PRICE", "QTY", "PCT", "RATIO", "BIG_AMOUNT", "Decimal"]
