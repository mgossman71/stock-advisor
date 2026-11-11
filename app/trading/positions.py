"""Position arithmetic: the single source of truth for P&L.

Formulas (mirrored by the docstrings in ``app/db/models_trading.py`` and
unit-tested in ``tests/test_pnl_math.py``):

* **Long add** reweights the average cost, buy commission included (so the
  commission shows up in realised P&L, matching what left the cash balance)::

      new_avg = (old_qty * old_avg + fill_qty * fill_price + commission) / new_qty

* **Shorts** keep a *negative* ``qty``; ``avg_cost`` is the price the shares
  were sold into, net of the opening commission::

      new_avg = (old_qty_abs * old_avg + fill_qty * fill_price - commission) / new_qty_abs

* **Realised P&L closing a long**::

      (fill_price - avg_cost) * qty_closed - commission

* **Realised P&L closing a short**::

      (avg_cost - fill_price) * qty_closed - commission

  Borrow is not part of this formula: the nightly accrual debits it from cash
  and from ``realized_pnl`` day by day while the short is open
  (``app.trading.orders.accrue_short_borrow``).

* **Unrealised P&L** is ``None`` when no mark price is known.  It is never 0:
  0 would read as "flat" while an absent price really means "we don't know".
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

ZERO = Decimal("0")


@dataclass(frozen=True)
class Fill:
    """One fill to apply to a position (signed impact decided by ``side``)."""

    side: str  # buy | sell | short | cover
    qty: Decimal
    price: Decimal
    commission: Decimal = ZERO


def apply_fill(
    qty: Decimal,
    avg_cost: Decimal | None,
    fill: Fill,
) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    """Return ``(new_qty, new_avg_cost, realised_pnl_delta, borrow_delta)``.

    ``qty`` is signed (negative = short).  ``realised_pnl_delta`` and
    ``borrow_delta`` are the amounts to add to the position's running totals;
    neither is ever guessed - both are exact from the fill inputs.
    """
    if fill.qty <= 0:
        raise ValueError("fill qty must be positive")
    if fill.price < 0:
        raise ValueError("fill price cannot be negative")

    if fill.side == "buy":
        if qty > 0:  # add to long: reweight average cost
            base = avg_cost if avg_cost is not None else ZERO
            new_qty = qty + fill.qty
            new_avg = (qty * base + fill.qty * fill.price + fill.commission) / new_qty
            return new_qty, new_avg, ZERO, ZERO
        if qty < 0:
            raise ValueError("cannot buy into a short position - use cover")
        return fill.qty, (fill.qty * fill.price + fill.commission) / fill.qty, ZERO, ZERO

    if fill.side == "short":
        if qty < 0:  # add to short: reweight entry price
            base = avg_cost if avg_cost is not None else ZERO
            new_qty = qty - fill.qty
            new_avg = (-qty * base + fill.qty * fill.price - fill.commission) / -new_qty
            return new_qty, new_avg, ZERO, ZERO
        if qty > 0:
            raise ValueError("cannot short into a long position - use sell")
        return -fill.qty, (fill.qty * fill.price - fill.commission) / fill.qty, ZERO, ZERO

    if fill.side == "sell":
        if qty <= 0:
            raise ValueError("cannot sell without a long position")
        if avg_cost is None:  # pragma: no cover - guarded at the DB layer
            raise ValueError("long position is missing avg_cost")
        closed = min(fill.qty, qty)
        realised = (fill.price - avg_cost) * closed - fill.commission
        new_qty = qty - closed
        return (new_qty, None if new_qty == 0 else avg_cost, realised, ZERO)

    if fill.side == "cover":
        if qty >= 0:
            raise ValueError("cannot cover without a short position")
        if avg_cost is None:  # pragma: no cover - guarded at the DB layer
            raise ValueError("short position is missing avg_cost")
        closed = min(fill.qty, -qty)
        # borrow_delta stays 0 here: accrual lives in the nightly job, which
        # has already taken it out of cash and realized_pnl day by day.
        realised = (avg_cost - fill.price) * closed - fill.commission
        new_qty = qty + closed
        return (new_qty, None if new_qty == 0 else avg_cost, realised, ZERO)

    raise ValueError(f"unknown side {fill.side!r}")


def position_market_value(qty: Decimal, last_price: Decimal | None) -> Decimal | None:
    """Mark-to-market value of a position; ``None`` when the price is unknown."""
    if last_price is None:
        return None
    return qty * last_price


def unrealized_pnl(position) -> Decimal | None:
    """Mark-to-market P&L of an open position.

    Long:  ``(last - avg_cost) * qty``
    Short: ``(avg_cost - last) * qty_magnitude``  (``qty`` is negative, so the
    sign falls out of ``last*qty - avg_cost*qty``)
    """
    from app.db.models_trading import Position

    if not isinstance(position, Position):  # pragma: no cover - defensive
        raise TypeError("expected a Position row")
    if position.qty == 0 or position.last_price is None or position.avg_cost is None:
        return None
    return position.last_price * position.qty - position.avg_cost * position.qty
