"""Account bootstrap and access.

v1 is single-user: exactly one ``Account`` row (id = 1).  The row is created
lazily from settings so any entry point - CLI, web, seeder, nightly job - can
call ``get_or_open_account`` without ordering concerns.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.models import Account


def get_or_open_account(session: Session, settings: Settings | None = None) -> Account:
    """Return the paper account, opening it with ``INITIAL_CASH`` if absent."""
    settings = settings or get_settings()
    account = session.scalar(select(Account).order_by(Account.id).limit(1))
    if account is None:
        account = Account(
            initial_cash=Decimal(settings.initial_cash),
            cash_balance=Decimal(settings.initial_cash),
        )
        session.add(account)
        session.flush()
    return account
