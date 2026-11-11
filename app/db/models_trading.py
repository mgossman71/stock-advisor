"""Paper-trading tables: account, orders, positions, snapshots, alerts.

P&L formulas (implemented in :mod:`app.trading.positions`, tested in
``tests/test_pnl_math.py``) mirror what these columns store:

* Long average cost reweighted on each add:
  ``new_avg = (old_qty*old_avg + fill_qty*fill_price) / new_qty``
* Short positions keep a *negative* ``qty``; ``avg_cost`` is the price the
  shares were sold into.
* Realised P&L closing a long:  ``(fill_price - avg_cost) * qty_closed - commission``
* Realised P&L closing a short: ``(avg_cost - fill_price) * qty_closed - commission``
* Borrow cost accrues daily on open shorts (``app.trading.orders.accrue_short_borrow``)
  and is charged straight into ``realized_pnl``, so a short's realized figure
  is always net of borrow; ``borrow_cost`` keeps the accrual visible.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.models_advice import Recommendation
from app.db.models_market import Ticker
from app.db.types import MONEY, PCT, PRICE, QTY


class Account(Base):
    """Single-user paper account.  v1 keeps exactly one row (id = 1)."""

    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(80), default="Paper account")
    currency: Mapped[str] = mapped_column(String(8), default="USD", nullable=False)
    initial_cash: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    cash_balance: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    opened_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    orders: Mapped[list["Order"]] = relationship(back_populates="account")
    positions: Mapped[list["Position"]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Account {self.name} cash={self.cash_balance}>"


class Order(Base):
    """A simulated order and its fill.  No real brokerage is contacted, ever."""

    __tablename__ = "orders"
    __table_args__ = (Index("ix_orders_submitted_at", "submitted_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    ticker_id: Mapped[int] = mapped_column(
        ForeignKey("tickers.id", ondelete="RESTRICT"), index=True
    )
    recommendation_id: Mapped[int | None] = mapped_column(
        ForeignKey("recommendations.id", ondelete="SET NULL"), index=True
    )
    #: ``buy`` | ``sell`` (close long) | ``short`` | ``cover``.
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    #: ``market`` | ``limit``.
    order_type: Mapped[str] = mapped_column(String(8), nullable=False, default="market")
    qty: Mapped[Decimal] = mapped_column(QTY, nullable=False)
    limit_price: Mapped[Decimal | None] = mapped_column(PRICE)
    submitted_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    fill_price: Mapped[Decimal | None] = mapped_column(PRICE)
    fill_date: Mapped[dt.date | None] = mapped_column(Date)
    #: Reference price the slippage model was applied to.
    reference_price: Mapped[Decimal | None] = mapped_column(PRICE)
    commission: Mapped[Decimal | None] = mapped_column(MONEY)
    slippage: Mapped[Decimal | None] = mapped_column(MONEY)
    #: ``filled`` | ``pending`` | ``rejected`` | ``cancelled``.
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="filled")
    reject_reason: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)

    account: Mapped[Account] = relationship(back_populates="orders")
    ticker: Mapped[Ticker] = relationship()


class Position(Base):
    """Aggregated holding.  ``qty`` is signed: negative means short."""

    __tablename__ = "positions"
    __table_args__ = (
        UniqueConstraint("account_id", "ticker_id", name="uq_positions_account_ticker"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    ticker_id: Mapped[int] = mapped_column(
        ForeignKey("tickers.id", ondelete="RESTRICT"), index=True
    )
    qty: Mapped[Decimal] = mapped_column(QTY, nullable=False, default=Decimal("0"))
    avg_cost: Mapped[Decimal | None] = mapped_column(PRICE)
    realized_pnl: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal("0"))
    borrow_cost: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal("0"))
    last_price: Mapped[Decimal | None] = mapped_column(PRICE)
    last_price_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    #: First fill date - used for "days held".
    opened_at: Mapped[dt.date | None] = mapped_column(Date)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    account: Mapped[Account] = relationship(back_populates="positions")
    ticker: Mapped[Ticker] = relationship()

    @property
    def is_short(self) -> bool:
        """True when net exposure is short."""
        return self.qty < 0

    @property
    def unrealized_pnl(self) -> Decimal | None:
        """Mark-to-market P&L; ``None`` when no price is known (never 0-guess)."""
        from app.trading.positions import unrealized_pnl

        return unrealized_pnl(self)


class PortfolioSnapshot(Base):
    """One row per calendar day: equity curve + benchmark close."""

    __tablename__ = "portfolio_snapshots"
    __table_args__ = (UniqueConstraint("snapshot_date", name="uq_snapshots_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    snapshot_date: Mapped[dt.date] = mapped_column(Date, nullable=False, index=True)
    cash: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    positions_value: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    total_value: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    spx_close: Mapped[Decimal | None] = mapped_column(PRICE)
    daily_pnl: Mapped[Decimal | None] = mapped_column(MONEY)
    #: Cumulative return vs. initial cash, in percent points.
    total_return_pct: Mapped[Decimal | None] = mapped_column(PCT)
    short_exposure: Mapped[Decimal | None] = mapped_column(MONEY)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CashAdjustment(Base):
    """Deposit (+) or withdrawal (-) of paper cash, requested by the user.

    Deliberately NOT an order: a deposit is not a trade and never counts
    toward realized P&L or the ``total_return_pct`` base.  ``daily_pnl`` in
    snapshots nets these out so a deposit cannot masquerade as profit.
    """

    __tablename__ = "cash_adjustments"
    __table_args__ = (Index("ix_cash_adjustments_created_at", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    #: Signed amount in account currency: positive = deposit, negative = withdrawal.
    delta: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    note: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    account: Mapped[Account] = relationship()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<CashAdjustment {self.delta:+}>"


class Alert(Base):
    """User- or system-created alert; triggered ones surface as the UI badge."""

    __tablename__ = "alerts"
    __table_args__ = (Index("ix_alerts_ack_triggered", "acknowledged", "triggered_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticker_id: Mapped[int | None] = mapped_column(
        ForeignKey("tickers.id", ondelete="CASCADE"), index=True
    )
    #: price_above | price_below | drawdown_pct | earnings_within | news
    type: Mapped[str] = mapped_column(String(24), nullable=False)
    threshold: Mapped[Decimal | None] = mapped_column(PRICE)
    message: Mapped[str] = mapped_column(String(400), nullable=False)
    #: Observed value that satisfied the alert (never fabricated).
    observed_value: Mapped[Decimal | None] = mapped_column(PRICE)
    observed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    triggered_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledged: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    ticker: Mapped[Ticker | None] = relationship()


class RealTrade(Base):
    """A buy or sell the user *actually executed* at a real broker.

    Deliberately separate from the paper ``orders`` table: paper fills are
    simulations, these rows are facts about real money.  They are never
    netted against paper cash and they feed the FIFO cost-basis and
    tax-liability math in :mod:`app.tracking`.  ``recommendation_id`` is
    the audit link back to the call that (maybe) drove the decision.
    """

    __tablename__ = "real_trades"
    __table_args__ = (Index("ix_real_trades_ticker_date", "ticker_id", "trade_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticker_id: Mapped[int] = mapped_column(
        ForeignKey("tickers.id", ondelete="CASCADE"), index=True
    )
    recommendation_id: Mapped[int | None] = mapped_column(
        ForeignKey("recommendations.id", ondelete="SET NULL"), index=True
    )
    #: buy | sell
    side: Mapped[str] = mapped_column(String(4), nullable=False)
    qty: Mapped[Decimal] = mapped_column(QTY, nullable=False)
    price: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    commission: Mapped[Decimal] = mapped_column(
        MONEY, nullable=False, default=Decimal("0")
    )
    #: User-entered execution date (defaults to today in the app layer).
    trade_date: Mapped[dt.date] = mapped_column(Date, nullable=False, index=True)
    note: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    ticker: Mapped[Ticker] = relationship()
    recommendation: Mapped[Recommendation | None] = relationship()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<RealTrade {self.side} {self.qty} @ {self.price} {self.trade_date}>"

