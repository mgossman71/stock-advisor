"""Holding-period classification and estimated tax liability.

Rates live in ``app_settings`` as fractions (``0.32`` = 32%):
``tax_rate_short_term`` (ordinary income rate, held one year or less) and
``tax_rate_long_term``.

* **Holding period** follows the IRS rule: long-term only when sold *after*
  the one-year anniversary of the purchase (calendar, so leap years count).
* **Netting** follows Schedule D: short-term and long-term are netted within
  their bucket, then a net loss in one bucket offsets a net gain in the other
  (taxed at the surviving bucket's rate).  A net capital loss owes nothing;
  up to ``LOSS_DEDUCTION_LIMIT`` of it offsets ordinary income and the rest
  carries forward - surfaced, not folded into a negative "tax".
* **Realized** figures cover sells in the *current tax year* only (gains from
  earlier years were already filed).  "If sold today" is the *extra* tax that
  liquidating every open lot would add on top of this year's realized result.

Estimates only - not tax advice; wash sales and carryforwards from earlier
years are out of scope for v1.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import Session

from app.db.models import AppSetting
from app.tracking.lots import holdings, open_lots, realized_matches

ZERO = Decimal("0")

#: Net capital loss usable against ordinary income per year (USD, single/joint).
LOSS_DEDUCTION_LIMIT = Decimal("3000")

RATE_KEYS = {"short": "tax_rate_short_term", "long": "tax_rate_long_term"}
DEFAULT_RATES = {"short": Decimal("0.32"), "long": Decimal("0.15")}


class TaxRateError(ValueError):
    """Rejected rate input (not a number, or outside 0..60%)."""


def _anniversary(bought: dt.date) -> dt.date:
    try:
        return bought.replace(year=bought.year + 1)
    except ValueError:  # bought Feb 29: the anniversary is Feb 28
        return dt.date(bought.year + 1, 2, 28)


def holding_class(bought: dt.date, sold: dt.date) -> str:
    """``long`` only when sold strictly after the purchase's one-year anniversary."""
    return "long" if sold > _anniversary(bought) else "short"


def net_capital_tax(st_net: Decimal, lt_net: Decimal,
                    rates: dict[str, Decimal]) -> tuple[Decimal, Decimal]:
    """Schedule-D style netting -> ``(tax, net_capital_loss)``.

    ``st_net``/``lt_net`` are signed bucket nets.  ``tax`` is never negative;
    ``net_capital_loss`` is the (positive) loss left after netting, if any.
    """
    if st_net >= 0 and lt_net >= 0:
        return st_net * rates["short"] + lt_net * rates["long"], ZERO
    if st_net < 0 and lt_net < 0:
        return ZERO, -(st_net + lt_net)
    total = st_net + lt_net
    if total <= 0:
        return ZERO, -total
    # One bucket's loss is absorbed by the other's gain; the remainder keeps
    # the rate of the bucket that had the gain.
    rate = rates["short"] if st_net > 0 else rates["long"]
    return total * rate, ZERO


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
        klass = holding_class(trade.trade_date, as_of)
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
    """Bucket totals for the page: unrealized (if sold today) + realized this tax year."""
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
        if m.sell.trade_date.year != as_of.year:
            continue  # an earlier tax year: already filed
        if m.sell.trade_date > as_of:
            continue
        key = ("st" if holding_class(m.buy.trade_date, m.sell.trade_date) == "short"
               else "lt") + ("_gain_real" if m.gross_gain > 0 else "_loss_real")
        buckets[key] += abs(m.gross_gain)

    def money(x: Decimal) -> Decimal:
        return x.quantize(Decimal("0.01"))

    st_real = buckets["st_gain_real"] - buckets["st_loss_real"]
    lt_real = buckets["lt_gain_real"] - buckets["lt_loss_real"]
    st_unreal = buckets["st_gain_unreal"] - buckets["st_loss_unreal"]
    lt_unreal = buckets["lt_gain_unreal"] - buckets["lt_loss_unreal"]
    real_tax, real_loss = net_capital_tax(st_real, lt_real, rates)
    all_tax, all_loss = net_capital_tax(st_real + st_unreal, lt_real + lt_unreal, rates)

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
        "tax_year": as_of.year,
        # Marginal: what selling everything today adds to this year's bill.
        "est_tax_if_sold_today": money(max(ZERO, all_tax - real_tax)),
        "est_tax_realized": money(real_tax),
        "net_capital_loss_realized": money(real_loss),
        "loss_deduction_limit": LOSS_DEDUCTION_LIMIT,
    }
