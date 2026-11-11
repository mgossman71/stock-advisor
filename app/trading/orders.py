"""Order execution against stored market data - simulated fills only.

The fill model:

* Reference price = the latest *stored* close for the ticker (bar or quote
  row).  If no price exists the order is **rejected**: we never price a fill
  from nothing.
* Slippage moves the fill against the trader: buys/covers pay
  ``ref * (1 + bps/10000)``, sells/shorts receive ``ref * (1 - bps/10000)``.
* Commission = ``max(commission_per_share * qty, min_commission)``.
* Cash legs: buy/cover debit ``qty*fill + commission``; sell/short credit
  ``qty*fill - commission``.  A buy or cover that would overdraw cash is
  rejected.
* Sides are strict: ``buy`` only opens/adds longs, ``sell`` only closes them,
  ``short`` only opens/adds shorts, ``cover`` only closes them.  This keeps the
  P&L attribution in ``apply_fill`` unambiguous.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.models import Account, Order, Position, PriceHistory, Ticker
from app.trading.positions import Fill, apply_fill

ZERO = Decimal("0")
SIDES = ("buy", "sell", "short", "cover")


@dataclass(frozen=True)
class ExecutionResult:
    """Outcome of one execute_order call (never raises on business rejects)."""

    order: Order
    accepted: bool
    reason: str | None = None


def latest_price(session: Session, ticker_id: int) -> tuple[Decimal, dt.date] | None:
    """Latest stored close for a ticker, with the bar's date as the as-of.

    Returns ``None`` when the DB holds no price - callers must treat that as
    "cannot execute", not "zero".
    """
    row = session.execute(
        select(PriceHistory.close, PriceHistory.date)
        .where(PriceHistory.ticker_id == ticker_id)
        .order_by(PriceHistory.date.desc())
        .limit(1)
    ).first()
    if row is None or row.close is None:
        return None
    return row.close, row.date


def _round_fill(price: Decimal, side: str) -> Decimal:
    """Quantise a fill price to 6dp, rounding *against* the trader."""
    mode = ROUND_CEILING if side in ("buy", "cover") else ROUND_FLOOR
    return price.quantize(Decimal("0.000001"), rounding=mode)


def compute_fill_inputs(
    reference: Decimal, qty: Decimal, side: str, settings: Settings
) -> tuple[Decimal, Decimal, Decimal]:
    """Return ``(fill_price, commission, slippage_cost)`` for a hypothetical fill."""
    bps = Decimal(settings.slippage_bps)
    if side in ("buy", "cover"):
        fill = reference * (Decimal("1") + bps / Decimal("10000"))
    else:
        fill = reference * (Decimal("1") - bps / Decimal("10000"))
    fill = _round_fill(fill, side)
    slippage_cost = abs(fill - reference) * qty
    commission = max(Decimal(settings.commission_per_share) * qty,
                     Decimal(settings.min_commission))
    return fill, commission, slippage_cost


def execute_order(
    session: Session,
    account: Account,
    symbol: str,
    side: str,
    qty: Decimal | int,
    *,
    settings: Settings | None = None,
    note: str | None = None,
    recommendation_id: int | None = None,
    limit_price: Decimal | None = None,
) -> ExecutionResult:
    """Execute a paper order end to end; returns the order row + verdict.

    Rejections (bad side, unknown price, insufficient cash, closing more than
    is open) still persist the order with ``status='rejected'`` and a reason,
    so the trade history shows the attempt.
    """
    settings = settings or get_settings()
    qty = Decimal(qty)
    if side not in SIDES:
        raise ValueError(f"side must be one of {SIDES}")
    if qty <= 0:
        raise ValueError("qty must be positive")

    ticker = session.scalar(select(Ticker).where(Ticker.symbol == symbol.upper()))
    priced = latest_price(session, ticker.id) if ticker else None

    order = Order(
        account_id=account.id,
        ticker_id=ticker.id if ticker else 0,
        recommendation_id=recommendation_id,
        side=side,
        order_type="limit" if limit_price is not None else "market",
        qty=qty,
        limit_price=limit_price,
        status="pending",
        note=note,
    )

    if ticker is None:
        # ticker_id is NOT NULL, so an attempt at an unknown symbol cannot be
        # persisted; the rejection is reported to the caller instead.
        order.status, order.reject_reason = "rejected", f"unknown ticker {symbol!r}"
        return ExecutionResult(order=order, accepted=False, reason=order.reject_reason)

    if priced is None:
        order.status, order.reject_reason = "rejected", "no stored price - cannot fill"
        session.add(order)
        session.flush()
        return ExecutionResult(order=order, accepted=False, reason=order.reject_reason)

    reference, as_of = priced
    fill_price, commission, slippage_cost = compute_fill_inputs(reference, qty, side, settings)

    # Limit discipline: a buy/cover fills only at or below the limit, a
    # sell/short only at or above it.  Otherwise it stays `pending`.
    if limit_price is not None:
        if side in ("buy", "cover") and fill_price > limit_price:
            order.status, order.fill_price, order.reference_price = "pending", None, reference
            session.add(order)
            session.flush()
            return ExecutionResult(
                order=order, accepted=False,
                reason=f"limit {limit_price} below fill {fill_price}",
            )
        if side in ("sell", "short") and fill_price < limit_price:
            order.status, order.fill_price, order.reference_price = "pending", None, reference
            session.add(order)
            session.flush()
            return ExecutionResult(
                order=order, accepted=False,
                reason=f"limit {limit_price} above fill {fill_price}",
            )

    position = session.scalar(
        select(Position).where(
            Position.account_id == account.id, Position.ticker_id == ticker.id
        )
    )
    pos_qty = position.qty if position else ZERO

    # Cash discipline for debit sides.
    debit = qty * fill_price + commission
    if side in ("buy", "cover") and account.cash_balance - debit < ZERO:
        order.status, order.reject_reason = "rejected", (
            f"insufficient cash: need {debit:.2f}, have {account.cash_balance:.2f}"
        )
        session.add(order)
        session.flush()
        return ExecutionResult(order=order, accepted=False, reason=order.reject_reason)

    # Closing sides cannot exceed the open position.
    if side == "sell" and (pos_qty <= 0 or qty > pos_qty):
        order.status, order.reject_reason = "rejected", (
            f"cannot sell {qty} with {pos_qty} open long"
        )
        session.add(order)
        session.flush()
        return ExecutionResult(order=order, accepted=False, reason=order.reject_reason)
    if side == "cover" and (pos_qty >= 0 or qty > -pos_qty):
        order.status, order.reject_reason = "rejected", (
            f"cannot cover {qty} with {-pos_qty} open short"
        )
        session.add(order)
        session.flush()
        return ExecutionResult(order=order, accepted=False, reason=order.reject_reason)

    # Apply the fill to the position.
    if position is None:
        position = Position(account_id=account.id, ticker_id=ticker.id, qty=ZERO)
        session.add(position)
        session.flush()
    new_qty, new_avg, realised, _ = apply_fill(
        pos_qty, position.avg_cost,
        Fill(side=side, qty=qty, price=fill_price, commission=commission),
    )
    position.qty = new_qty
    position.avg_cost = new_avg
    position.realized_pnl = position.realized_pnl + realised
    position.last_price = fill_price
    position.last_price_at = dt.datetime(
        as_of.year, as_of.month, as_of.day, tzinfo=dt.timezone.utc
    )
    if position.opened_at is None:
        position.opened_at = as_of

    # Cash leg.
    if side in ("buy", "cover"):
        account.cash_balance = account.cash_balance - debit
    else:
        account.cash_balance = account.cash_balance + (qty * fill_price - commission)

    order.fill_price, order.fill_date = fill_price, as_of
    order.reference_price, order.commission, order.slippage = reference, commission, slippage_cost
    order.status = "filled"
    session.add(order)
    session.flush()
    return ExecutionResult(order=order, accepted=True)


def accrue_short_borrow(
    session: Session, account: Account, today: dt.date, *, settings: Settings | None = None
) -> Decimal:
    """Charge one day of borrow cost on every open short.

    Cost = ``short_market_value * short_borrow_apy / 365``.  Skipped (never
    fabricated) when the short has no mark price.  Idempotent per day is the
    caller's job - the nightly wrapper only runs this once per calendar date.
    """
    settings = settings or get_settings()
    apy = Decimal(settings.short_borrow_apy)
    total = ZERO
    positions = session.scalars(
        select(Position).where(Position.account_id == account.id, Position.qty < 0)
    )
    for position in positions:
        if position.last_price is None:
            continue
        cost = (-position.qty) * position.last_price * apy / Decimal("365")
        position.borrow_cost = position.borrow_cost + cost
        position.realized_pnl = position.realized_pnl - cost
        total += cost
    session.flush()
    return total


def realized_pnl_by_ticker(session: Session, account: Account) -> dict[str, Decimal]:
    """Realised P&L per symbol from filled orders' positions (net of borrow)."""
    from app.db.models import Position

    out: dict[str, Decimal] = {}
    rows = session.execute(
        select(Position, Ticker.symbol).where(
            Position.account_id == account.id,
            Position.realized_pnl != 0,
        ).join(Ticker, Position.ticker_id == Ticker.id)
    )
    for position, symbol in rows:
        out[symbol] = position.realized_pnl
    return out
