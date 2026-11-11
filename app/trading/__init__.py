"""Paper-trading core: accounts, order execution, positions, snapshots.

All arithmetic here is :class:`~decimal.Decimal` - nothing that touches P&L is
allowed to run on binary floats.  Fill prices and mark prices must come from
stored market data; when a price is unknown the order is *rejected*, never
priced with a guess.
"""

from app.trading.accounts import get_or_open_account
from app.trading.orders import ExecutionResult, execute_order
from app.trading.positions import (
    apply_fill,
    position_market_value,
    unrealized_pnl,
)
from app.trading.sizing import suggested_size
from app.trading.snapshots import mark_positions, write_snapshot

__all__ = [
    "ExecutionResult",
    "apply_fill",
    "execute_order",
    "get_or_open_account",
    "mark_positions",
    "position_market_value",
    "suggested_size",
    "unrealized_pnl",
    "write_snapshot",
]
