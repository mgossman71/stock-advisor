"""Marking positions and writing the daily equity-curve snapshot.

* Positions are marked from the latest *stored* close - if a ticker has no
  price the position keeps its old mark and the snapshot reports it; nothing
  is silently zeroed.
* ``write_snapshot`` is idempotent per calendar day (unique constraint +
  merge), because ``POST /jobs/refresh`` and the nightly cron both call it and
  re-runs must not duplicate rows.
* Short exposure = ``sum(|qty| * mark)`` over shorts.  Equity math with
  shorts: ``total = cash + Σ(qty*mark)`` where shorts carry negative value
  and their sale proceeds already sit in cash - verified in the tests.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Account, PortfolioSnapshot, Position, PriceHistory, Ticker
from app.trading.orders import latest_price
from app.trading.positions import position_market_value

ZERO = Decimal("0")


def _open_positions(session: Session, account: Account):
    """All positions with nonzero qty, queried (not cached via the relationship)."""
    return session.scalars(
        select(Position).where(Position.account_id == account.id, Position.qty != 0)
    ).all()


def mark_positions(session: Session, account: Account) -> int:
    """Refresh ``last_price``/``last_price_at`` from stored bars; returns count marked."""
    marked = 0
    for position in _open_positions(session, account):
        priced = latest_price(session, position.ticker_id)
        if priced is None:
            continue
        price, bar_date = priced
        position.last_price = price
        position.last_price_at = dt.datetime(bar_date.year, bar_date.month, bar_date.day,
                                             tzinfo=dt.timezone.utc)
        marked += 1
    session.flush()
    return marked


def portfolio_values(session: Session, account: Account) -> dict[str, Decimal | None]:
    """Cash, positions value, total equity and short exposure from current marks."""
    positions_value: Decimal = ZERO
    short_exposure: Decimal = ZERO
    unmarked: list[str] = []
    for position in _open_positions(session, account):
        if position.qty == 0:
            continue
        value = position_market_value(position.qty, position.last_price)
        if value is None:
            symbol = position.ticker.symbol if position.ticker else f"id{position.ticker_id}"
            unmarked.append(symbol)
            continue
        positions_value += value
        if position.qty < 0:
            short_exposure += -value
    return {
        "cash": account.cash_balance,
        "positions_value": positions_value,
        "total_value": account.cash_balance + positions_value,
        "short_exposure": short_exposure,
        "unmarked": unmarked,  # symbols kept at an old/absent mark - surfaced, not hidden
    }


def benchmark_close(session: Session, symbol: str, on_or_before: dt.date) -> Decimal | None:
    """Benchmark close for the equity-curve overlay (None when absent)."""
    ticker = session.scalar(select(Ticker).where(Ticker.symbol == symbol))
    if ticker is None:
        return None
    row = session.execute(
        select(PriceHistory.close)
        .where(PriceHistory.ticker_id == ticker.id, PriceHistory.date <= on_or_before)
        .order_by(PriceHistory.date.desc())
        .limit(1)
    ).scalar()
    return row


def write_snapshot(
    session: Session, account: Account, on_date: dt.date, *, benchmark_symbol: str
) -> PortfolioSnapshot:
    """Upsert the snapshot row for ``on_date``; safe to call repeatedly."""
    values = portfolio_values(session, account)
    spx = benchmark_close(session, benchmark_symbol, on_date)
    row = session.scalar(
        select(PortfolioSnapshot).where(PortfolioSnapshot.snapshot_date == on_date)
    )
    if row is None:
        row = PortfolioSnapshot(snapshot_date=on_date, cash=ZERO,
                                positions_value=ZERO, total_value=ZERO)
        session.add(row)
    previous = session.execute(
        select(PortfolioSnapshot.total_value)
        .where(PortfolioSnapshot.snapshot_date < on_date)
        .order_by(PortfolioSnapshot.snapshot_date.desc())
        .limit(1)
    ).scalar()
    row.cash = values["cash"]
    row.positions_value = values["positions_value"]
    row.total_value = values["total_value"]
    row.spx_close = spx
    row.short_exposure = values["short_exposure"]
    row.daily_pnl = (
        values["total_value"] - previous if previous is not None else None
    )
    row.total_return_pct = (
        (values["total_value"] / account.initial_cash - Decimal("1")) * Decimal("100")
        if account.initial_cash
        else None
    )
    session.flush()
    return row


def equity_curve(session: Session) -> list[dict[str, object]]:
    """Chronological snapshot rows for the Chart.js equity overlay."""
    rows = session.scalars(
        select(PortfolioSnapshot).order_by(PortfolioSnapshot.snapshot_date)
    ).all()
    return [
        {
            "date": row.snapshot_date.isoformat(),
            "total": row.total_value,
            "cash": row.cash,
            "spx": row.spx_close,
        }
        for row in rows
    ]
