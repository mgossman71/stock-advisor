"""FIFO lot matching for the real-money ledger.

Everything here is :class:`~decimal.Decimal`.  Trades are replayed in
(``trade_date``, ``id``) order per ticker: buys push lots, sells pop them
FIFO.  Cost basis carries the buy commission (added) and proceeds carry the
sell commission (subtracted), allocated pro-rata per share so partial fills
across lots stay exact.
"""

from __future__ import annotations

import datetime as dt
from collections import deque
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import RealTrade, Ticker
from app.ingestion.ingest import ensure_ticker

ZERO = Decimal("0")


class RealTradeError(ValueError):
    """Rejected real-trade entry (bad quantity/price, or a sell that would
    exceed what the ledger says is actually held)."""


@dataclass
class OpenLot:
    """One still-open buy lot: original trade, remaining qty, unit cost."""

    trade: RealTrade
    remaining: Decimal
    cost_per_share: Decimal  # includes the buy commission, per share


@dataclass(frozen=True)
class RealizedMatch:
    """A FIFO pairing of N shares from a buy lot against a sell."""

    sell: RealTrade
    buy: RealTrade
    qty: Decimal
    proceeds_per_share: Decimal  # sell price less sell commission, per share
    cost_per_share: Decimal

    @property
    def gross_gain(self) -> Decimal:
        return (self.proceeds_per_share - self.cost_per_share) * self.qty

    @property
    def held_days(self) -> int:
        return (self.sell.trade_date - self.buy.trade_date).days


def _buy_cost_per_share(t: RealTrade) -> Decimal:
    return (t.qty * t.price + t.commission) / t.qty


def _sell_proceeds_per_share(t: RealTrade) -> Decimal:
    return (t.qty * t.price - t.commission) / t.qty


def _replay(trades: list[RealTrade]) -> tuple[list[OpenLot], list[RealizedMatch]]:
    """Single chronological pass -> open lots (per symbol, FIFO order) + matches."""
    queues: dict[int, deque[OpenLot]] = {}
    matches: list[RealizedMatch] = []
    for t in sorted(trades, key=lambda t: (t.ticker_id, t.trade_date, t.id)):
        if t.side == "buy":
            queues.setdefault(t.ticker_id, deque()).append(
                OpenLot(t, t.qty, _buy_cost_per_share(t))
            )
            continue
        queue = queues.setdefault(t.ticker_id, deque())
        need, proceeds = t.qty, _sell_proceeds_per_share(t)
        while need > 0 and queue:
            lot = queue[0]
            take = min(need, lot.remaining)
            matches.append(RealizedMatch(t, lot.trade, take, proceeds, lot.cost_per_share))
            lot.remaining -= take
            need -= take
            if lot.remaining == 0:
                queue.popleft()
        if need > 0:  # defensive: record_trade validates, but a corrupt ledger must not silently pass
            raise RealTradeError(
                f"ledger inconsistent: sell of trade #{t.id} exceeds open quantity "
                f"by {need} shares"
            )
    return [lot for q in queues.values() for lot in q], matches


def _all_trades(session: Session) -> list[RealTrade]:
    return session.scalars(
        select(RealTrade).order_by(RealTrade.ticker_id, RealTrade.trade_date, RealTrade.id)
    ).all()


def open_lots(session: Session, symbol: str | None = None) -> list[OpenLot]:
    """Open lots FIFO-oldest-first, optionally filtered by one symbol."""
    lots, _ = _replay(_all_trades(session))
    if symbol:
        lots = [lot for lot in lots if lot.trade.ticker.symbol == symbol.upper()]
    return lots


def realized_matches(session: Session) -> list[RealizedMatch]:
    """Every FIFO buy->sell pairing implied by the ledger so far."""
    _, matches = _replay(_all_trades(session))
    return matches


def holdings(session: Session) -> list[dict[str, object]]:
    """Per-symbol roll-up: qty, total cost basis, latest stored mark (or None)."""
    from app.trading.orders import latest_price  # local import: keep tracking independent of orders' weight

    lots, _ = _replay(_all_trades(session))
    by_symbol: dict[str, dict[str, object]] = {}
    for lot in lots:
        symbol = lot.trade.ticker.symbol
        entry = by_symbol.setdefault(
            symbol,
            {"symbol": symbol, "ticker_id": lot.trade.ticker_id, "qty": ZERO,
             "cost_basis": ZERO, "price": None, "price_date": None,
             "unrealized": None},
        )
        entry["qty"] = entry["qty"] + lot.remaining
        entry["cost_basis"] = entry["cost_basis"] + lot.remaining * lot.cost_per_share
    for entry in by_symbol.values():
        priced = latest_price(session, int(entry["ticker_id"]))
        if priced is not None:
            price, bar_date = priced
            entry["price"], entry["price_date"] = price, bar_date
            entry["unrealized"] = entry["qty"] * price - entry["cost_basis"]
    return sorted(by_symbol.values(), key=lambda e: str(e["symbol"]))


def open_quantity(session: Session, ticker_id: int) -> Decimal:
    lots, _ = _replay(_all_trades(session))
    return sum((lot.remaining for lot in lots if lot.trade.ticker_id == ticker_id), ZERO)


def record_trade(
    session: Session,
    symbol: str,
    side: str,
    qty: Decimal,
    price: Decimal,
    *,
    trade_date: dt.date | None = None,
    commission: Decimal = ZERO,
    recommendation_id: int | None = None,
    note: str | None = None,
) -> RealTrade:
    """Validate + insert one real buy/sell.  Sells cannot exceed open quantity."""
    side = side.lower()
    if side not in ("buy", "sell"):
        raise RealTradeError("side must be buy or sell")
    qty, price = Decimal(qty), Decimal(price)
    if qty <= 0:
        raise RealTradeError("quantity must be positive")
    if price <= 0:
        raise RealTradeError("price must be positive (you know what you paid)")
    if Decimal(commission) < 0:
        raise RealTradeError("commission cannot be negative")
    ticker = ensure_ticker(session, symbol)
    if side == "sell" and qty > open_quantity(session, ticker.id):
        held = open_quantity(session, ticker.id)
        raise RealTradeError(
            f"cannot sell {qty:,.4f} {ticker.symbol}: only {held:,.4f} held "
            f"in the real ledger"
        )
    trade = RealTrade(
        ticker_id=ticker.id, recommendation_id=recommendation_id, side=side,
        qty=qty, price=price, commission=Decimal(commission),
        trade_date=trade_date or dt.date.today(), note=note,
    )
    session.add(trade)
    session.flush()
    return trade
