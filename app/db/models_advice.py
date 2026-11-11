"""Advice tables: research reports, recommendations, graded outcomes, config."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import (
    JSON,
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
from app.db.models_market import Ticker
from app.db.types import PCT, PRICE, RATIO


class ResearchReport(Base):
    """Full evidence-backed report.  ``payload_json`` is the canonical report."""

    __tablename__ = "research_reports"
    __table_args__ = (Index("ix_research_reports_created_at", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticker_id: Mapped[int] = mapped_column(
        ForeignKey("tickers.id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    #: Which rule set produced it, e.g. ``rules-v1`` (an LLM engine bumps this).
    model_or_rule_version: Mapped[str] = mapped_column(String(60), nullable=False)
    #: Latest timestamp of any data point inside the report.
    data_as_of: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    #: bullish | neutral | bearish
    overall_stance: Mapped[str] = mapped_column(String(12), nullable=False)
    score: Mapped[Decimal] = mapped_column(RATIO, nullable=False)
    #: bull_points, bear_points, risks, evidence[], sub_scores, missing_fields...
    payload_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    ticker: Mapped[Ticker] = relationship()
    recommendations: Mapped[list["Recommendation"]] = relationship(
        back_populates="report", cascade="all, delete-orphan"
    )


class ScanScore(Base):
    """One stock's cheap nightly scan score (no AI, no news/analyst calls).

    The funnel's first stage: every universe member is scored from stored
    prices + the latest fundamentals snapshot; the top ``shortlist_size``
    (``rank``) get the full fetch, report and optional LLM review.  ``rank``
    is NULL for names that could not be scored on enough data.
    """

    __tablename__ = "scan_scores"
    __table_args__ = (
        UniqueConstraint("ticker_id", "scan_date", name="uq_scan_scores_ticker_date"),
        Index("ix_scan_scores_scan_date_rank", "scan_date", "rank"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticker_id: Mapped[int] = mapped_column(
        ForeignKey("tickers.id", ondelete="CASCADE"), index=True
    )
    scan_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    #: Weighted composite of the known sub-scores (same weights as reports).
    score: Mapped[Decimal] = mapped_column(RATIO, nullable=False)
    #: {"value": "61.20" | null, "momentum": ..., "quality": ..., "sentiment": ..., "risk": ...}
    sub_scores: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    #: False = price-only score (fundamentals not fetched yet); ranked after
    #: every name that has them.
    has_fundamentals: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    rank: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    ticker: Mapped[Ticker] = relationship()


class Recommendation(Base):
    """A dated LONG or SHORT call that the app later grades against SPX."""

    __tablename__ = "recommendations"
    __table_args__ = (
        Index("ix_recommendations_status_generated", "status", "generated_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticker_id: Mapped[int] = mapped_column(
        ForeignKey("tickers.id", ondelete="CASCADE"), index=True
    )
    report_id: Mapped[int | None] = mapped_column(
        ForeignKey("research_reports.id", ondelete="SET NULL"), index=True
    )
    #: ``long`` | ``short``
    direction: Mapped[str] = mapped_column(String(6), nullable=False, index=True)
    #: ``short`` (<3m) | ``medium`` (3-12m) | ``long`` (>12m)
    horizon: Mapped[str] = mapped_column(String(8), nullable=False, default="medium")
    generated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    entry_price_at_call: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    stop_price: Mapped[Decimal | None] = mapped_column(PRICE)
    target_price: Mapped[Decimal | None] = mapped_column(PRICE)
    #: open | hit_stop | hit_target | closed | expired
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="open")
    #: 0-100, from composite score magnitude + data completeness.
    confidence: Mapped[Decimal] = mapped_column(PCT, nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    #: JSON list of the specific data points the call cites.
    evidence_json: Mapped[list | None] = mapped_column(JSON)
    closed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    #: Which AdvisorEngine emitted the call.
    engine_version: Mapped[str] = mapped_column(String(60), nullable=False, default="rules-v1")

    ticker: Mapped[Ticker] = relationship()
    report: Mapped[ResearchReport | None] = relationship(back_populates="recommendations")
    outcome: Mapped["RecommendationOutcome | None"] = relationship(
        back_populates="recommendation", cascade="all, delete-orphan", uselist=False
    )


class RecommendationOutcome(Base):
    """30/90/180-day grading of a call vs. the benchmark, same windows."""

    __tablename__ = "recommendation_outcomes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    recommendation_id: Mapped[int] = mapped_column(
        ForeignKey("recommendations.id", ondelete="CASCADE"), unique=True, index=True
    )
    price_at_30d: Mapped[Decimal | None] = mapped_column(PRICE)
    price_at_90d: Mapped[Decimal | None] = mapped_column(PRICE)
    price_at_180d: Mapped[Decimal | None] = mapped_column(PRICE)
    benchmark_price_at_30d: Mapped[Decimal | None] = mapped_column(PRICE)
    benchmark_price_at_90d: Mapped[Decimal | None] = mapped_column(PRICE)
    benchmark_price_at_180d: Mapped[Decimal | None] = mapped_column(PRICE)
    #: Signed call return in the window (already direction-correct for shorts).
    outcome_pct_30d: Mapped[Decimal | None] = mapped_column(PCT)
    outcome_pct_90d: Mapped[Decimal | None] = mapped_column(PCT)
    outcome_pct_180d: Mapped[Decimal | None] = mapped_column(PCT)
    benchmark_pct_30d: Mapped[Decimal | None] = mapped_column(PCT)
    benchmark_pct_90d: Mapped[Decimal | None] = mapped_column(PCT)
    benchmark_pct_180d: Mapped[Decimal | None] = mapped_column(PCT)
    alpha_30d: Mapped[Decimal | None] = mapped_column(PCT)
    alpha_90d: Mapped[Decimal | None] = mapped_column(PCT)
    alpha_180d: Mapped[Decimal | None] = mapped_column(PCT)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    recommendation: Mapped[Recommendation] = relationship(back_populates="outcome")


class ScoringWeight(Base):
    """User-tunable composite weights; validated to sum to 1.0 in the app layer."""

    __tablename__ = "scoring_weights"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    weight: Mapped[Decimal] = mapped_column(RATIO, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class AppSetting(Base):
    """Key/value store for UI-editable settings (weights, alert defaults...)."""

    __tablename__ = "app_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(60), unique=True, nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class NotificationChannel(Base):
    """Hook point for v2 email/webhook delivery (disabled in v1)."""

    __tablename__ = "notification_channels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # email | webhook
    target: Mapped[str] = mapped_column(String(300), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

