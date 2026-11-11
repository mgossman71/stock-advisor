"""Holding-period classification and estimated tax liability.

Rates live in ``app_settings`` as fractions (``0.32`` = 32%):
``tax_rate_short_term`` (ordinary income rate applied to <=365-day gains) and
``tax_rate_long_term``.  Unrealized figures answer "if I sold *today*";
realized figures classify each FIFO match by the holding period at sale.
Losses reduce the estimate (assumes no other capital gains/losses offset).
Estimates only - not tax advice; wash sales are out of scope for v1.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import Session

from app.db.models import AppSetting
from app.tracking.lots import holdings, open_lots, realized_matches

ZERO = Decimal("0")

#: <= 365 days held = short term; > 365 = long term (calendar days, UTC-agnostic).
LONG_TERM_MIN_DAYS = 366

RATE_KEYS = {"short": "tax_rate_short_term", "long": "tax_rate_long_term"}
DEFAULT_RATES = {"short": Decimal("0.32"), "long": Decimal("0.15")}


class TaxRateError(ValueError):
    """Rejected rate input (not a number, or outside 0..60%)."""


def holding_class(held_days: int) -> str:
    return "long" if held_days >= LONG_TERM_MIN_DAYS else "short"


def get_tax_rates(session: Session) -> dict[str, Decimal]:
    rates = dict(DEFAULT_RATES)
    for klass, key in RATE_KEYS.items():
        row = session.query(AppSetting).filter_by(key=key).first()
        if row is not None:
            try:
                rates[klass] = Decimal(row.value)
            except InvalidOperation:  # pragma: no cover - defensive
                pass
    return rates


def set_tax_rates(session: Session, short: Decimal, long: Decimal) -> None:
    for klass, value in (("short", Decimal(short)), ("long", Decimal(long))):
        if not (ZERO <= value <= Decimal("0.60")):
            raise TaxRateError(f"{klass} rate must be between 0% and 60%")
        row = session.query(AppSetting).filter_by(key=RATE_KEYS[klass]).first()
        if row is None:
            row = AppSetting(key=RATE_KEYS[klass], value=str(value))
            session.add(row)
        else:
            row.value = str(value)
    session.flush()


@dataclass(frozen=True)
class LotValuation:
    """One open lot, marked and tax-classified as of ``as_of``."""

    symbol: str
    trade_date: dt.date
    held_days: int
    klass: str  # short | long
    qty: Decimal
    cost_basis: Decimal
    price: Decimal | None  # None = no stored bar: surfaced, never guessed
    price_date: dt.date | None
    market_value: Decimal | None
    unrealized: Decimal | None
    est_tax_if_sold: Decimal | None  # on a gain; gains only, losses show 0


def lot_valuations(session: Session, as_of: dt.date) -> list[LotValuation]:
    from app.trading.orders import latest_price

    rates = get_tax_rates(session)
    out: list[LotValuation] = []
    for lot in open_lots(session):
        trade = lot.trade
        held = (as_of - trade.trade_date).days
        klass = holding_class(held)
        basis = lot.remaining * lot.cost_per_share
        priced = latest_price(session, trade.ticker_id)
        if priced is None:
            out.append(LotValuation(trade.ticker.symbol, trade.trade_date, held,
                                    klass, lot.remaining, basis,
                                    None, None, None, None, None))
            continue
        price, bar_date = priced
        value = lot.remaining * price
        gain = value - basis
        tax = gain * rates[klass] if gain > 0 else ZERO
        out.append(LotValuation(trade.ticker.symbol, trade.trade_date, held, klass,
                                lot.remaining, basis, price, bar_date, value,
                                gain, tax))
    return out


def tax_summary(session: Session, as_of: dt.date) -> dict[str, object]:
    """Bucket totals for the page: unrealized (if sold today) + realized so far."""
    rates = get_tax_rates(session)
    buckets = {k: ZERO for k in
               ("st_gain_unreal", "lt_gain_unreal", "st_loss_unreal", "lt_loss_unreal",
                "st_gain_real", "lt_gain_real", "st_loss_real", "lt_loss_real")}

    for v in lot_valuations(session, as_of):
        if v.unrealized is None:
            continue
        key = ("st" if v.klass == "short" else "lt") + ("_gain_unreal" if v.unrealized > 0
                                                        else "_loss_unreal")
        buckets[key] += abs(v.unrealized)

    for m in realized_matches(session):
        key = ("st" if holding_class(m.held_days) == "short" else "lt") + \
              ("_gain_real" if m.gross_gain > 0 else "_loss_real")
        buckets[key] += abs(m.gross_gain)

    def money(x: Decimal) -> Decimal:
        return x.quantize(Decimal("0.01"))

    unreal_tax = (buckets["st_gain_unreal"] - buckets["st_loss_unreal"]) * rates["short"] + \
                 (buckets["lt_gain_unreal"] - buckets["lt_loss_unreal"]) * rates["long"]
    real_tax = (buckets["st_gain_real"] - buckets["st_loss_real"]) * rates["short"] + \
               (buckets["lt_gain_real"] - buckets["lt_loss_real"]) * rates["long"]

    marked = holdings(session)
    return {
        **buckets,
        "cost_basis": sum((h["cost_basis"] for h in marked), ZERO),
        "market_value": sum((h["cost_basis"] + (h["unrealized"] or ZERO)
                             for h in marked), ZERO),
        "unrealized": sum((h["unrealized"] or ZERO for h in marked), ZERO),
        "unrealized_unmarked": sum(
            (h["cost_basis"] for h in marked if h["unrealized"] is None), ZERO),
        "rates": rates,
        "est_tax_if_sold_today": money(unreal_tax),
        "est_tax_realized": money(real_tax),
    }
