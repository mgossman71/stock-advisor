"""Stock-split detection and the one-time rescale it triggers.

Providers serve split-adjusted closes, so after a split a re-fetched range
disagrees with what is stored by a constant factor on every bar before the
split, and agrees on every bar after it.  ``detect_split`` looks for exactly
that shape; anything messier (a single corrected bar, provider noise) is left
alone rather than guessed at.

``apply_split`` then moves everything priced in the old share basis onto the
new one, once: stored bars, dividends per share, open paper positions,
pending limit orders, recommendations and untriggered price alerts.  Real trades are *not*
rewritten - the ledger replays them through the ``stock_splits`` rows.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from fractions import Fraction

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    Alert, Dividend, Order, Position, PriceHistory, Recommendation, StockSplit,
)

#: A factor this close to 1 is a data revision, not a split.
MIN_SPLIT_DEVIATION = Decimal("0.05")
#: Bars on the same side of the split must agree this closely.
AGREEMENT = Decimal("0.01")
#: The factor must be this close to a small-integer ratio (2:1, 3:2, 1:10 ...).
RATIO_TOLERANCE = Decimal("0.002")
MAX_RATIO_TERM = 50
#: Pre-split bars needed before a factor is believed.
MIN_PRE_SPLIT_BARS = 2

_PRICE_Q = Decimal("0.000001")


def detect_split(
    stored: dict[dt.date, Decimal], fresh: dict[dt.date, Decimal]
) -> tuple[dt.date, int, int] | None:
    """Return ``(split_date, split_from, split_to)`` or ``None``.

    ``stored``/``fresh`` map date -> close for settled bars already in the DB
    and bars just returned by the provider.  ``split_date`` is the first fresh
    bar after the last pre-split stored bar.
    """
    common = sorted(d for d in stored if d in fresh and stored[d] > 0 and fresh[d] > 0)
    ratios = [(d, stored[d] / fresh[d]) for d in common]
    off = [d for d, r in ratios if abs(r - 1) > MIN_SPLIT_DEVIATION]
    if not off:
        return None
    last_off = off[-1]
    before = [r for d, r in ratios if d <= last_off]
    after = [r for d, r in ratios if d > last_off]
    if len(before) < MIN_PRE_SPLIT_BARS:
        return None
    factor = sorted(before)[len(before) // 2]
    if any(abs(r / factor - 1) > AGREEMENT for r in before):
        return None  # not one constant factor: a revision, not a split
    if any(abs(r - 1) > AGREEMENT for r in after):
        return None
    frac = Fraction(factor).limit_denominator(MAX_RATIO_TERM)
    if frac.numerator > MAX_RATIO_TERM or frac.numerator == frac.denominator:
        return None
    nice = Decimal(frac.numerator) / Decimal(frac.denominator)
    if abs(nice / factor - 1) > RATIO_TOLERANCE:
        return None
    split_date = next((d for d in sorted(fresh) if d > last_off), None)
    if split_date is None:
        return None
    # stored/fresh = new shares per old share = split_to / split_from
    return split_date, frac.denominator, frac.numerator


def _rescale(value: Decimal | None, price_factor: Decimal) -> Decimal | None:
    return None if value is None else (value * price_factor).quantize(_PRICE_Q)


def apply_split(
    session: Session, ticker_id: int, split_date: dt.date, split_from: int,
    split_to: int, *, source: str,
) -> StockSplit | None:
    """Record the split and rescale old-basis rows; no-op if already recorded."""
    existing = session.scalar(
        select(StockSplit).where(StockSplit.ticker_id == ticker_id,
                                 StockSplit.split_date == split_date)
    )
    if existing is not None:
        return None
    price_factor = Decimal(split_from) / Decimal(split_to)
    qty_factor = Decimal(split_to) / Decimal(split_from)

    for bar in session.scalars(
        select(PriceHistory).where(PriceHistory.ticker_id == ticker_id,
                                   PriceHistory.date < split_date)
    ):
        bar.open = _rescale(bar.open, price_factor)
        bar.high = _rescale(bar.high, price_factor)
        bar.low = _rescale(bar.low, price_factor)
        bar.close = _rescale(bar.close, price_factor)
        bar.adj_close = _rescale(bar.adj_close, price_factor)
        if bar.volume is not None:
            bar.volume = int(Decimal(bar.volume) * qty_factor)

    for div in session.scalars(
        select(Dividend).where(Dividend.ticker_id == ticker_id, Dividend.ex_date < split_date)
    ):
        div.amount = _rescale(div.amount, price_factor)

    for pos in session.scalars(
        select(Position).where(Position.ticker_id == ticker_id, Position.qty != 0)
    ):
        if pos.opened_at is not None and pos.opened_at >= split_date:
            continue  # opened on the new basis already
        pos.qty = pos.qty * qty_factor
        pos.avg_cost = _rescale(pos.avg_cost, price_factor)
        pos.last_price = _rescale(pos.last_price, price_factor)

    for order in session.scalars(
        select(Order).where(Order.ticker_id == ticker_id, Order.status == "pending")
    ):
        order.qty = order.qty * qty_factor
        order.limit_price = _rescale(order.limit_price, price_factor)

    for rec in session.scalars(
        select(Recommendation).where(Recommendation.ticker_id == ticker_id)
    ):
        if rec.generated_at.date() >= split_date:
            continue
        rec.entry_price_at_call = _rescale(rec.entry_price_at_call, price_factor)
        rec.stop_price = _rescale(rec.stop_price, price_factor)
        rec.target_price = _rescale(rec.target_price, price_factor)

    for alert in session.scalars(
        select(Alert).where(Alert.ticker_id == ticker_id, Alert.triggered_at.is_(None),
                            Alert.type.in_(("price_above", "price_below")))
    ):
        if alert.created_at is not None and alert.created_at.date() >= split_date:
            continue
        alert.threshold = _rescale(alert.threshold, price_factor)

    split = StockSplit(ticker_id=ticker_id, split_date=split_date,
                       split_from=split_from, split_to=split_to, source=source)
    session.add(split)
    session.flush()
    return split


def splits_by_ticker(session: Session) -> dict[int, list[StockSplit]]:
    """All recorded splits, oldest first, keyed by ticker id."""
    out: dict[int, list[StockSplit]] = {}
    for split in session.scalars(select(StockSplit).order_by(StockSplit.split_date)):
        out.setdefault(split.ticker_id, []).append(split)
    return out
