"""Real-money tracking: FIFO cost basis + tax-liability estimates.

This package deliberately has *nothing* to do with the paper account: it
answers "what did I actually buy, what is it worth, and what would I owe if
I sold today / what did I realize when I sold".  Lots are matched FIFO
(the IRS default when no election is made); classification is short-term
(<= 365 days held) vs long-term (> 365 days).  Every "tax" figure is an
estimate at the user-configured rates, not tax advice, and wash-sale rules
are explicitly out of scope for v1.
"""

from app.tracking.lots import (
    OpenLot,
    RealTradeError,
    RealizedMatch,
    holdings,
    open_lots,
    record_trade,
    realized_matches,
)
from app.tracking.taxes import (
    get_tax_rates,
    lot_valuations,
    set_tax_rates,
    tax_summary,
)

__all__ = [
    "OpenLot",
    "RealTradeError",
    "RealizedMatch",
    "get_tax_rates",
    "holdings",
    "lot_valuations",
    "open_lots",
    "record_trade",
    "realized_matches",
    "set_tax_rates",
    "tax_summary",
]
