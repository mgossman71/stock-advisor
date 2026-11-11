"""Background work: the nightly pipeline and its manual trigger.

One pass = fetch what matters -> score/screen -> grade outcomes -> accrue
borrow -> mark positions -> snapshot the day -> evaluate alerts.

Idempotency is the contract (acceptance criterion #4): ``POST /jobs/refresh``
twice in a row must be safe.  It is, because ingestion upserts on
(ticker, date), screening keeps one open call per (ticker, direction),
grading rewrites the same outcome fields, and snapshots merge per date.
Borrow accrual is the only *additive* step, and it is gated per calendar day
via the fetch-log so a double trigger cannot double-charge.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.models import (
    Alert, DataFetchLog, EarningsEvent, PriceHistory, Recommendation, Ticker,
)
from app.ingestion.ingest import log_fetch, refresh_symbol
from app.providers.base import MarketDataProvider
from app.research.outcomes import grade_recommendation, update_statuses
from app.research.screener import screen_universe
from app.trading.accounts import get_or_open_account
from app.trading.orders import accrue_short_borrow
from app.trading.snapshots import mark_positions, write_snapshot

D = Decimal


@dataclass
class JobReport:
    """Human-readable summary of one nightly pass, returned by the endpoint."""

    fetched: list[str] = field(default_factory=list)
    fetch_errors: list[str] = field(default_factory=list)
    scored: int = 0
    new_recommendations: int = 0
    graded: int = 0
    statuses: dict[str, int] = field(default_factory=dict)
    borrow_charged: Decimal = D("0")
    snapshot_date: str | None = None
    alerts_triggered: int = 0
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "fetched": self.fetched, "fetch_errors": self.fetch_errors,
            "scored": self.scored, "new_recommendations": self.new_recommendations,
            "graded": self.graded, "statuses": self.statuses,
            "borrow_charged": str(self.borrow_charged),
            "snapshot_date": self.snapshot_date,
            "alerts_triggered": self.alerts_triggered, "notes": self.notes,
        }


def target_symbols(session: Session, settings: Settings) -> list[str]:
    """Everything the nightly job must keep fresh: universe + watchlist +
    open positions + open recommendations + benchmark + VIX."""
    symbols: set[str] = set()
    symbols.update(session.scalars(
        select(Ticker.symbol).where((Ticker.in_universe.is_(True))
                                    | (Ticker.in_watchlist.is_(True)))
    ))
    from app.db.models import Position

    account = session.scalar(select(Position.ticker_id).limit(1))
    if account is not None:
        symbols.update(session.scalars(
            select(Ticker.symbol).join(Position, Position.ticker_id == Ticker.id)
            .where(Position.qty != 0)
        ))
    symbols.update(session.scalars(
        select(Ticker.symbol).join(Recommendation, Recommendation.ticker_id == Ticker.id)
        .where(Recommendation.status == "open")
    ))
    symbols.update({settings.benchmark_symbol, settings.vix_symbol})
    return sorted(symbols)


def _borrow_already_charged(session: Session, today: dt.date) -> bool:
    return session.scalar(
        select(func.count(DataFetchLog.id)).where(
            DataFetchLog.provider == "system",
            DataFetchLog.endpoint == "short_borrow",
            func.date(DataFetchLog.fetched_at) == today.isoformat(),
        )
    ) > 0


def run_nightly(
    session: Session, provider: MarketDataProvider, *,
    settings: Settings | None = None, today: dt.date | None = None,
    max_symbols: int | None = None, with_alternatives: bool = False,
) -> JobReport:
    """Full pipeline pass.  Safe to call any number of times per day."""
    settings = settings or get_settings()
    today = today or dt.date.today()
    report = JobReport()

    # 1. fetch (only what we already track - universe loading is a separate step)
    symbols = target_symbols(session, settings)
    if max_symbols:
        symbols = symbols[:max_symbols]
    for symbol in symbols:
        asset = "index" if symbol in (settings.benchmark_symbol, settings.vix_symbol) else "stock"
        try:
            refresh_symbol(session, provider, symbol, with_alternatives=with_alternatives,
                           asset_class=asset)
            report.fetched.append(symbol)
        except Exception as exc:  # provider chain already retries; keep the job alive
            report.fetch_errors.append(f"{symbol}: {exc}")
        session.commit()

    # 2. borrow accrual, once per calendar day
    account = get_or_open_account(session, settings)
    if not _borrow_already_charged(session, today):
        report.borrow_charged = accrue_short_borrow(session, account, today, settings=settings)
        entry = log_fetch(session, provider="system", endpoint="short_borrow", ticker=None,
                          status="ok", row_count=int(report.borrow_charged != 0))
        # Anchor the marker row to the *job* date, not wall-clock now, so a
        # re-run for the same calendar day is gated identically.
        entry.fetched_at = dt.datetime.combine(
            today, dt.time(0, 0, 1), tzinfo=dt.timezone.utc
        )
        session.commit()
    else:
        report.notes.append("borrow already accrued today - skipped")

    # 3. mark + snapshot
    mark_positions(session, account)
    snapshot = write_snapshot(session, account, today, benchmark_symbol=settings.benchmark_symbol)
    report.snapshot_date = snapshot.snapshot_date.isoformat()
    session.commit()

    # 4. screen + grade
    screen = screen_universe(session)
    report.scored = screen.scored
    report.new_recommendations = len(screen.new_recommendations)
    for rec in session.scalars(select(Recommendation)):
        if grade_recommendation(session, rec, benchmark_symbol=settings.benchmark_symbol,
                                today=today) is not None:
            report.graded += 1
    report.statuses = update_statuses(session, today=today)
    session.commit()

    # 5. alerts
    report.alerts_triggered = evaluate_alerts(session, settings, today=today)
    session.commit()
    return report


def evaluate_alerts(session: Session, settings: Settings, *,
                    today: dt.date | None = None) -> int:
    """Trigger unacknowledged alerts against stored data (no provider calls)."""
    today = today or dt.date.today()
    triggered = 0
    alerts = session.scalars(
        select(Alert).where(Alert.triggered_at.is_(None), Alert.acknowledged.is_(False))
    ).all()
    for alert in alerts:
        symbol = alert.ticker.symbol if alert.ticker else None
        if symbol is None:
            continue
        last_bar = session.execute(
            select(PriceHistory.close, PriceHistory.date)
            .where(PriceHistory.ticker_id == alert.ticker_id)
            .order_by(PriceHistory.date.desc())
            .limit(1)
        ).first()
        if last_bar is None:
            continue
        price, bar_date = last_bar
        hit: tuple[str, D] | None = None
        if alert.type == "price_above" and alert.threshold is not None and price >= alert.threshold:
            hit = (f"{symbol} closed at {price} >= {alert.threshold}", price)
        elif alert.type == "price_below" and alert.threshold is not None and price <= alert.threshold:
            hit = (f"{symbol} closed at {price} <= {alert.threshold}", price)
        elif alert.type == "earnings_within":
            soon = session.scalar(
                select(EarningsEvent).where(
                    EarningsEvent.ticker_id == alert.ticker_id,
                    EarningsEvent.status == "scheduled",
                    EarningsEvent.event_date >= today,
                    EarningsEvent.event_date <= today + dt.timedelta(
                        days=int(alert.threshold or settings.alert_default_earnings_within_days)),
                ).limit(1)
            )
            if soon:
                hit = (f"{symbol} reports earnings on {soon.event_date}", None)
        if hit:
            alert.message = hit[0]
            alert.observed_value = hit[1]
            alert.observed_at = dt.datetime(bar_date.year, bar_date.month, bar_date.day,
                                            tzinfo=dt.timezone.utc)
            alert.triggered_at = dt.datetime.now(dt.timezone.utc)
            triggered += 1
    session.flush()
    return triggered
