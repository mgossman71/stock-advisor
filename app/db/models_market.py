"""Market-data tables: tickers, prices, fundamentals.

Conventions used throughout:

* ``NULL`` means "the provider did not give us this value" - it is never filled
  with a zero or an interpolated number.
* Every table holding provider output records ``source`` so a report can cite
  exactly where a number came from.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
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
from app.db.types import BIG_AMOUNT, PCT, PRICE, RATIO


class Ticker(Base):
    """One tradable instrument (stock, ETF or index)."""

    __tablename__ = "tickers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    name: Mapped[str | None] = mapped_column(String(200))
    sector: Mapped[str | None] = mapped_column(String(100), index=True)
    industry: Mapped[str | None] = mapped_column(String(120))
    market_cap: Mapped[Decimal | None] = mapped_column(BIG_AMOUNT)
    currency: Mapped[str | None] = mapped_column(String(8))
    #: ``stock`` | ``etf`` | ``index`` - drives provider symbol conventions.
    asset_class: Mapped[str] = mapped_column(String(12), default="stock", nullable=False)
    in_universe: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    in_watchlist: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    prices: Mapped[list["PriceHistory"]] = relationship(
        back_populates="ticker", cascade="all, delete-orphan"
    )
    fundamentals: Mapped[list["Fundamentals"]] = relationship(
        back_populates="ticker", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Ticker {self.symbol} id={self.id}>"


class PriceHistory(Base):
    """One daily bar.  Unique on (ticker, date) so nightly re-runs upsert."""

    __tablename__ = "price_history"
    __table_args__ = (
        UniqueConstraint("ticker_id", "date", name="uq_price_history_ticker_date"),
        Index("ix_price_history_date", "date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticker_id: Mapped[int] = mapped_column(
        ForeignKey("tickers.id", ondelete="CASCADE"), index=True
    )
    date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    open: Mapped[Decimal | None] = mapped_column(PRICE)
    high: Mapped[Decimal | None] = mapped_column(PRICE)
    low: Mapped[Decimal | None] = mapped_column(PRICE)
    close: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    adj_close: Mapped[Decimal | None] = mapped_column(PRICE)
    volume: Mapped[int | None] = mapped_column(BigInteger)
    #: True when the row was priced from a live quote rather than a settled
    #: daily bar.  Provisional rows never count as "history is fresh", and the
    #: next history fetch overwrites them with the real bar.
    provisional: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    source: Mapped[str] = mapped_column(String(40), nullable=False, default="unknown")
    fetched_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    ticker: Mapped[Ticker] = relationship(back_populates="prices")

    @property
    def adjusted_close(self) -> Decimal:
        """Prefer adjusted close, else raw close (never invents a number)."""
        return self.adj_close if self.adj_close is not None else self.close


class Fundamentals(Base):
    """Point-in-time fundamental snapshot; one row per as-of date."""

    __tablename__ = "fundamentals"
    __table_args__ = (
        UniqueConstraint("ticker_id", "as_of_date", name="uq_fundamentals_ticker_asof"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticker_id: Mapped[int] = mapped_column(
        ForeignKey("tickers.id", ondelete="CASCADE"), index=True
    )
    as_of_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    pe_trailing: Mapped[Decimal | None] = mapped_column(RATIO)
    pe_forward: Mapped[Decimal | None] = mapped_column(RATIO)
    ev_ebitda: Mapped[Decimal | None] = mapped_column(RATIO)
    ps: Mapped[Decimal | None] = mapped_column(RATIO)
    pb: Mapped[Decimal | None] = mapped_column(RATIO)
    debt_to_equity: Mapped[Decimal | None] = mapped_column(RATIO)
    revenue_growth_yoy: Mapped[Decimal | None] = mapped_column(PCT)
    eps_growth_yoy: Mapped[Decimal | None] = mapped_column(PCT)
    dividend_yield: Mapped[Decimal | None] = mapped_column(PCT)
    fcf_yield: Mapped[Decimal | None] = mapped_column(PCT)
    gross_margin: Mapped[Decimal | None] = mapped_column(PCT)
    operating_margin: Mapped[Decimal | None] = mapped_column(PCT)
    profit_margin: Mapped[Decimal | None] = mapped_column(PCT)
    return_on_equity: Mapped[Decimal | None] = mapped_column(PCT)
    current_price: Mapped[Decimal | None] = mapped_column(PRICE)
    target_price_consensus: Mapped[Decimal | None] = mapped_column(PRICE)
    short_pct_float: Mapped[Decimal | None] = mapped_column(PCT)
    beta: Mapped[Decimal | None] = mapped_column(RATIO)
    fifty_two_week_high: Mapped[Decimal | None] = mapped_column(PRICE)
    fifty_two_week_low: Mapped[Decimal | None] = mapped_column(PRICE)
    #: Valuation snapshots carry market cap too, so a report can cite the market
    #: cap that was current on the snapshot's as-of date.
    market_cap: Mapped[Decimal | None] = mapped_column(BIG_AMOUNT)
    source: Mapped[str] = mapped_column(String(40), nullable=False, default="unknown")
    fetched_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    ticker: Mapped[Ticker] = relationship(back_populates="fundamentals")


class NewsItem(Base):
    """Headline-level news; sentiment is nullable and never guessed."""

    __tablename__ = "news_items"
    __table_args__ = (
        UniqueConstraint("ticker_id", "url", name="uq_news_items_ticker_url"),
        Index("ix_news_items_published_at", "published_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticker_id: Mapped[int] = mapped_column(
        ForeignKey("tickers.id", ondelete="CASCADE"), index=True
    )
    published_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    source: Mapped[str | None] = mapped_column(String(120))
    title: Mapped[str] = mapped_column(String(400), nullable=False)
    url: Mapped[str] = mapped_column(String(600), nullable=False)
    sentiment_score: Mapped[Decimal | None] = mapped_column(PCT)
    body_excerpt: Mapped[str | None] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(String(40), nullable=False, default="unknown")
    fetched_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class AnalystAction(Base):
    """A rating / price-target action by one firm on one date."""

    __tablename__ = "analyst_actions"
    __table_args__ = (
        UniqueConstraint(
            "ticker_id", "action_date", "firm", "action", name="uq_analyst_actions_dedupe"
        ),
        Index("ix_analyst_actions_date", "action_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticker_id: Mapped[int] = mapped_column(
        ForeignKey("tickers.id", ondelete="CASCADE"), index=True
    )
    action_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    firm: Mapped[str | None] = mapped_column(String(120))
    #: upgrade | downgrade | target_raise | target_cut | initiate | reassess
    action: Mapped[str] = mapped_column(String(24), nullable=False)
    old_rating: Mapped[str | None] = mapped_column(String(60))
    new_rating: Mapped[str | None] = mapped_column(String(60))
    old_target: Mapped[Decimal | None] = mapped_column(PRICE)
    new_target: Mapped[Decimal | None] = mapped_column(PRICE)
    source: Mapped[str] = mapped_column(String(40), nullable=False, default="unknown")
    fetched_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class EarningsEvent(Base):
    """Reported or scheduled earnings, with surprise where the provider gives it."""

    __tablename__ = "earnings_events"
    __table_args__ = (
        UniqueConstraint("ticker_id", "event_date", name="uq_earnings_events_ticker_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticker_id: Mapped[int] = mapped_column(
        ForeignKey("tickers.id", ondelete="CASCADE"), index=True
    )
    event_date: Mapped[dt.date] = mapped_column(Date, nullable=False, index=True)
    eps_estimate: Mapped[Decimal | None] = mapped_column(RATIO)
    revenue_estimate: Mapped[Decimal | None] = mapped_column(BIG_AMOUNT)
    eps_actual: Mapped[Decimal | None] = mapped_column(RATIO)
    revenue_actual: Mapped[Decimal | None] = mapped_column(BIG_AMOUNT)
    surprise_pct: Mapped[Decimal | None] = mapped_column(PCT)
    #: ``reported`` for past quarters, ``scheduled`` for upcoming.
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="scheduled")
    source: Mapped[str] = mapped_column(String(40), nullable=False, default="unknown")
    fetched_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class DataFetchLog(Base):
    """Audit trail for every provider call: freshness, debugging, rate-limit proof."""

    __tablename__ = "data_fetch_log"
    __table_args__ = (Index("ix_data_fetch_log_fetched_at", "fetched_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    endpoint: Mapped[str] = mapped_column(String(200), nullable=False)
    ticker: Mapped[str | None] = mapped_column(String(16), index=True)
    fetched_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    #: ok | empty | error | skipped_fresh
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    row_count: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)



class StockSplit(Base):
    """A detected share split: ``split_to`` new shares per ``split_from`` old.

    ``split_date`` is the first session priced on the new basis.  Stored bars,
    paper positions and calls are rescaled once when the split is recorded;
    the real-trade ledger is left as entered and adjusted on replay
    (``app/tracking/lots.py``), so the user's own records are never rewritten.
    """

    __tablename__ = "stock_splits"
    __table_args__ = (
        UniqueConstraint("ticker_id", "split_date", name="uq_stock_splits_ticker_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticker_id: Mapped[int] = mapped_column(
        ForeignKey("tickers.id", ondelete="CASCADE"), index=True
    )
    split_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    split_from: Mapped[int] = mapped_column(Integer, nullable=False)
    split_to: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[str] = mapped_column(String(40), nullable=False, default="unknown")
    detected_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    ticker: Mapped[Ticker] = relationship()


class Dividend(Base):
    """A cash dividend per share, keyed by ex-dividend date (split-adjusted)."""

    __tablename__ = "dividends"
    __table_args__ = (
        UniqueConstraint("ticker_id", "ex_date", name="uq_dividends_ticker_exdate"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticker_id: Mapped[int] = mapped_column(
        ForeignKey("tickers.id", ondelete="CASCADE"), index=True
    )
    ex_date: Mapped[dt.date] = mapped_column(Date, nullable=False, index=True)
    amount: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    source: Mapped[str] = mapped_column(String(40), nullable=False, default="unknown")
    fetched_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    ticker: Mapped[Ticker] = relationship()
