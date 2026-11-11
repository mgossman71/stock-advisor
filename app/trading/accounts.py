"""Account bootstrap, access, and user cash adjustments.

v1 is single-user: exactly one ``Account`` row (id = 1).  The row is created
lazily from settings so any entry point - CLI, web, seeder, nightly job - can
call ``get_or_open_account`` without ordering concerns.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.models import Account, CashAdjustment


class CashAdjustmentError(ValueError):
    """Rejected cash adjustment (would overdraw available cash, or is a no-op)."""


def get_or_open_account(
    session: Session,
    settings: Settings | None = None,
    initial_cash: Decimal | None = None,
) -> Account:
    """Return the paper account, opening it with ``INITIAL_CASH`` if absent.

    ``initial_cash`` overrides the configured starting balance for a brand-new
    account (ignored when the account already exists).
    """
    settings = settings or get_settings()
    account = session.scalar(select(Account).order_by(Account.id).limit(1))
    if account is None:
        cash = Decimal(initial_cash if initial_cash is not None
                       else settings.initial_cash)
        account = Account(initial_cash=cash, cash_balance=cash)
        session.add(account)
        session.flush()
    return account


def adjust_cash(
    session: Session,
    account: Account,
    delta: Decimal,
    note: str | None = None,
) -> CashAdjustment:
    """Deposit (``delta`` > 0) or withdraw (``delta`` < 0) account cash.

    Booked as an auditable :class:`CashAdjustment` and reflected in the cash
    balance immediately.  Never touches ``initial_cash``, so total-return and
    win-rate math is unaffected; snapshots exclude adjustments from
    ``daily_pnl``.  Withdrawals may not overdraw available cash.
    """
    delta = Decimal(delta)
    if delta == 0:
        raise CashAdjustmentError("adjustment amount must be non-zero")
    if account.cash_balance + delta < 0:
        raise CashAdjustmentError(
            f"withdrawal of {abs(delta):,.2f} exceeds available cash of "
            f"{account.cash_balance:,.2f}"
        )
    adjustment = CashAdjustment(account_id=account.id, delta=delta, note=note)
    session.add(adjustment)
    account.cash_balance = account.cash_balance + delta
    session.flush()
    return adjustment
