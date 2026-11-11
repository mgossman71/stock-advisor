"""Cash dividends on the paper account: longs are paid, shorts pay.

Entitlement follows the market rule: the holder at the close *before* the
ex-dividend date is owed the dividend.  That holding is reconstructed as the
position's current qty minus everything filled on or after the ex-date, so
"bought on the ex-date" earns nothing and "sold on the ex-date" still earns.

Booking happens on the ex-date (the economic date; brokers show it accrued),
not the later pay date.  Each (account, dividend) books once - the unique
index on ``dividend_payments`` is the guard, so the nightly job and
``POST /jobs/refresh`` can re-run freely.

Cash moves by ``shares * amount`` and so does the position's
``realized_pnl``, keeping realised P&L equal to the cash it explains.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Account, Dividend, DividendPayment, Order, Position

ZERO = Decimal("0")
#: How far back an un-booked ex-date is still honoured.
LOOKBACK_DAYS = 30
_SIGN = {"buy": 1, "cover": 1, "sell": -1, "short": -1}


def shares_entitled(session: Session, position: Position, ex_date: dt.date) -> Decimal:
    """Signed shares held at the close before ``ex_date`` (negative = short)."""
    after = session.scalars(
        select(Order).where(
            Order.account_id == position.account_id,
            Order.ticker_id == position.ticker_id,
            Order.status == "filled",
            Order.fill_date >= ex_date,
        )
    ).all()
    moved = sum((Decimal(_SIGN[o.side]) * o.qty for o in after), ZERO)
    return position.qty - moved


def book_dividends(session: Session, account: Account, today: dt.date) -> Decimal:
    """Book every owed, not-yet-booked dividend with an ex-date in the lookback.

    Returns the net cash moved (positive = credited).
    """
    since = today - dt.timedelta(days=LOOKBACK_DAYS)
    positions = {
        p.ticker_id: p for p in session.scalars(
            select(Position).where(Position.account_id == account.id)
        )
    }
    if not positions:
        return ZERO
    booked = set(session.scalars(
        select(DividendPayment.dividend_id).where(DividendPayment.account_id == account.id)
    ))
    dividends = session.scalars(
        select(Dividend).where(
            Dividend.ticker_id.in_(positions),
            Dividend.ex_date > since,
            Dividend.ex_date <= today,
        ).order_by(Dividend.ex_date)
    ).all()

    net = ZERO
    for div in dividends:
        if div.id in booked:
            continue
        position = positions[div.ticker_id]
        qty = shares_entitled(session, position, div.ex_date)
        if qty == 0:
            continue
        total = (qty * div.amount).quantize(Decimal("0.0001"))
        account.cash_balance = account.cash_balance + total
        position.realized_pnl = position.realized_pnl + total
        session.add(DividendPayment(account_id=account.id, dividend_id=div.id,
                                    qty=qty, total=total))
        net += total
    session.flush()
    return net
