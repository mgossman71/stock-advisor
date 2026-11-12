"""Background work: the nightly pipeline, its manual trigger, and the quick
price refresh.

``run_price_refresh`` is the cheap job ("Refresh prices"): one live quote per
symbol the user holds or watches, then fills/marks/snapshot/alerts.  No
history, fundamentals, news, screening or grading - those stay in the full
pass, which is what makes the full pass slow.

One pass is a funnel (see ``app.research.scan``): refresh the US-listed stock
list when due -> cheap history for every universe stock + a rotating slice of
fundamentals -> scan-score them all -> full fetch (news, analysts, earnings)
for the shortlist and everything held/watched -> accrue borrow -> book
dividends -> retry pending limit orders -> mark positions -> snapshot the day
-> full reports (+ LLM) for the shortlist -> grade outcomes -> evaluate alerts.

Idempotency is the contract (acceptance criterion #4): ``POST /jobs/refresh``
twice in a row must be safe.  It is, because ingestion upserts on
(ticker, date), screening keeps one open call per (ticker, direction),
grading rewrites the same outcome fields, and snapshots merge per date.
Borrow accrual is the only *additive* step, and it is gated per calendar day
via the fetch-log so a double trigger cannot double-charge.  Dividends are
additive too, but each (account, dividend) books once by unique index.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.models import (
    Alert, AppSetting, DataFetchLog, EarningsEvent, Order, PriceHistory, Recommendation,
    ResearchReport, Ticker,
)
from app.background import Progress
from app.db.base import session_scope
from app.ingestion.ingest import (
    earnings_calendar_days, ingest_earnings_calendar, ingest_fundamentals, ingest_history,
    ingest_quote, log_fetch, refresh_symbol,
)
from app.ingestion.universe import load_universe, persist_universe
from app.providers.registry import build_provider
from app.providers.base import MarketDataProvider
from app.research.backtest import run_backtest, save_result as save_backtest
from app.research.outcomes import grade_recommendation, update_statuses
from app.research.scan import (
    SCAN_LOOKBACK_DAYS, ScanResult, confirm_emerging, emerging_symbols, fundamentals_due,
    load_scan_config,
    record_universe_refresh, run_scan, shortlist_symbols, universe_due,
    universe_stock_symbols,
)
from app.research.screener import screen_universe
from app.trading.accounts import get_or_open_account
from app.tracking.lots import open_lots
from app.trading.dividends import book_dividends
from app.trading.orders import accrue_short_borrow, process_pending_orders
from app.trading.snapshots import mark_positions, write_snapshot

D = Decimal


@dataclass
class JobReport:
    """Human-readable summary of one nightly pass, returned by the endpoint."""

    fetched: list[str] = field(default_factory=list)
    fetch_errors: list[str] = field(default_factory=list)
    #: Funnel stage 1: universe stocks whose history was refreshed / scored.
    scan_fetched: int = 0
    scan_fetch_errors: int = 0
    #: Earnings-calendar days fetched / companies matched to tracked tickers.
    earnings_days: int = 0
    earnings_matched: int = 0
    fundamentals_refreshed: int = 0
    scan: dict = field(default_factory=dict)
    scored: int = 0
    new_recommendations: int = 0
    graded: int = 0
    statuses: dict[str, int] = field(default_factory=dict)
    borrow_charged: Decimal = D("0")
    dividends_booked: Decimal = D("0")
    orders: dict[str, int] = field(default_factory=dict)
    snapshot_date: str | None = None
    alerts_triggered: int = 0
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "fetched": self.fetched, "fetch_errors": self.fetch_errors,
            "scan_fetched": self.scan_fetched, "scan_fetch_errors": self.scan_fetch_errors,
            "fundamentals_refreshed": self.fundamentals_refreshed, "scan": self.scan,
            "scored": self.scored, "new_recommendations": self.new_recommendations,
            "graded": self.graded, "statuses": self.statuses,
            "borrow_charged": str(self.borrow_charged),
            "dividends_booked": str(self.dividends_booked),
            "orders": self.orders,
            "snapshot_date": self.snapshot_date,
            "alerts_triggered": self.alerts_triggered, "notes": self.notes,
        }


@dataclass
class PriceRefreshReport:
    """Summary of one quick price refresh."""

    symbols: int = 0
    quoted: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    orders: dict[str, int] = field(default_factory=dict)
    snapshot_date: str | None = None
    alerts_triggered: int = 0

    def as_dict(self) -> dict:
        return {"symbols": self.symbols, "quoted": self.quoted, "errors": self.errors,
                "orders": self.orders, "snapshot_date": self.snapshot_date,
                "alerts_triggered": self.alerts_triggered}


def price_symbols(session: Session, settings: Settings) -> list[str]:
    """What the user holds or watches: open paper positions, real-ledger
    holdings, pending orders, untriggered alerts, open recommendations,
    watchlist, benchmark.  Screening-only universe members are left out."""
    from app.db.models import Position

    symbols: set[str] = set()
    symbols.update(session.scalars(select(Ticker.symbol).where(Ticker.in_watchlist.is_(True))))
    symbols.update(session.scalars(
        select(Ticker.symbol).join(Position, Position.ticker_id == Ticker.id)
        .where(Position.qty != 0)
    ))
    symbols.update(lot.trade.ticker.symbol for lot in open_lots(session))
    symbols.update(session.scalars(
        select(Ticker.symbol).join(Order, Order.ticker_id == Ticker.id)
        .where(Order.status == "pending")
    ))
    symbols.update(session.scalars(
        select(Ticker.symbol).join(Alert, Alert.ticker_id == Ticker.id)
        .where(Alert.triggered_at.is_(None), Alert.acknowledged.is_(False))
    ))
    symbols.update(session.scalars(
        select(Ticker.symbol).join(Recommendation, Recommendation.ticker_id == Ticker.id)
        .where(Recommendation.status == "open")
    ))
    symbols.add(settings.benchmark_symbol)
    return sorted(symbols)


def target_symbols(session: Session, settings: Settings) -> list[str]:
    """What gets the *full* nightly fetch (news, analysts, earnings,
    dividends): the latest scan's shortlist and emerging candidates +
    everything ``price_symbols``
    covers + VIX.  The rest of the universe only gets cheap history
    (``scan_fetch_symbols``)."""
    symbols = set(price_symbols(session, settings))
    symbols.update(shortlist_symbols(session))
    symbols.update(emerging_symbols(session))
    symbols.add(settings.vix_symbol)
    return sorted(symbols)


def scan_fetch_symbols(session: Session, settings: Settings) -> list[str]:
    """Universe stocks that only need history for the scan (not held/watched -
    those get the full fetch anyway)."""
    held = set(price_symbols(session, settings))
    return [s for s in universe_stock_symbols(session) if s not in held]


def _borrow_already_charged(session: Session, today: dt.date) -> bool:
    return session.scalar(
        select(func.count(DataFetchLog.id)).where(
            DataFetchLog.provider == "system",
            DataFetchLog.endpoint == "short_borrow",
            func.date(DataFetchLog.fetched_at) == today.isoformat(),
        )
    ) > 0


def needs_first_run(session: Session) -> bool:
    """A fresh install: no screening universe yet, or most of it never scanned.

    The second half makes setup resumable - if the process stopped during the
    first fetch (or only the seed script ran, which researches a handful),
    the next start runs it again and ingestion skips what is already fresh.
    """
    from app.db.models import ScanScore

    universe = session.scalar(
        select(func.count(Ticker.id)).where(Ticker.in_universe.is_(True))) or 0
    scanned = session.scalar(
        select(func.count(func.distinct(ScanScore.ticker_id)))
        .join(Ticker, Ticker.id == ScanScore.ticker_id)
        .where(Ticker.in_universe.is_(True))) or 0
    return universe == 0 or scanned * 2 < universe


def run_pipeline(progress: Progress | None = None, *, max_symbols: int | None = None,
                 with_alternatives: bool = True) -> JobReport:
    """Background entry point: own session + provider, universe loaded if missing."""
    settings = get_settings()
    with session_scope() as session:
        return run_nightly(session, build_provider(settings), settings=settings,
                           max_symbols=max_symbols, with_alternatives=with_alternatives,
                           load_universe_if_empty=True, refresh_universe=True,
                           progress=progress)


def run_price_refresh(progress: Progress | None = None) -> PriceRefreshReport:
    """Background entry point for "Refresh prices": own session + provider."""
    settings = get_settings()
    with session_scope() as session:
        return refresh_prices(session, build_provider(settings), settings=settings,
                              progress=progress)


def refresh_prices(
    session: Session, provider: MarketDataProvider, *,
    settings: Settings | None = None, today: dt.date | None = None,
    progress: Progress | None = None,
) -> PriceRefreshReport:
    """Quote what the user holds/watches, then fill limits, mark, snapshot, alert.

    Idempotent like the full pass: quotes upsert the day's provisional bar
    (never a settled one), pending limits fill at most once, the snapshot
    merges per date.  Borrow and dividends are left to the full pass, which
    gates them per day.
    """
    settings = settings or get_settings()
    today = today or dt.date.today()
    report = PriceRefreshReport()
    symbols = price_symbols(session, settings)
    report.symbols = len(symbols)
    for index, symbol in enumerate(symbols, start=1):
        if progress is not None:
            progress("fetching prices", index - 1, len(symbols), symbol)
        asset = "index" if symbol in (settings.benchmark_symbol, settings.vix_symbol) else "stock"
        try:
            stats, quote = ingest_quote(session, provider, symbol, asset_class=asset)
        except Exception as exc:  # one bad symbol must not stop the rest
            report.errors.append(f"{symbol}: {exc}")
        else:
            if quote is None:
                report.errors.append(f"{symbol}: {'; '.join(stats.notes) or 'no quote'}")
            else:
                report.quoted.append(symbol)
        session.commit()

    if progress is not None:
        progress("marking positions")
    account = get_or_open_account(session, settings)
    report.orders = process_pending_orders(session, account, settings=settings)
    session.commit()
    mark_positions(session, account)
    snapshot = write_snapshot(session, account, today, benchmark_symbol=settings.benchmark_symbol)
    report.snapshot_date = snapshot.snapshot_date.isoformat()
    session.commit()
    report.alerts_triggered = evaluate_alerts(session, settings, today=today)
    session.commit()
    return report


def run_screen_only(progress: Progress | None = None):
    """Background entry point for "Screen now": re-scan stored data, then
    full reports for the shortlist.  No fetching."""
    with session_scope() as session:
        scan = run_scan(session, progress=progress)
        confirm_emerging(session)
        session.commit()
        stats = screen_universe(session, progress=progress, commit_each=True)
        stats.scanned = scan.scored
        session.commit()
        return stats


#: The lens backtest needs ~2 years of month-ends plus the 400-day scan
#: window behind each one, so history is extended to this once (the nightly
#: scan keeps fetching only its own window).
BACKTEST_HISTORY_YEARS = 3
HISTORY_EXTENDED_KEY = "history_extended_from"


def extend_history(session: Session, provider: MarketDataProvider, settings: Settings,
                   today: dt.date, progress: Progress | None = None) -> int:
    """Fetch older bars for the universe (+ benchmark) back to
    ``BACKTEST_HISTORY_YEARS``.  Runs once: a stamp in ``app_settings`` stops
    a re-run (young listings can never reach 3 years and would refetch
    forever).  Returns the number of symbols fetched."""
    start = today - dt.timedelta(days=365 * BACKTEST_HISTORY_YEARS)
    stamp = session.scalar(select(AppSetting).where(AppSetting.key == HISTORY_EXTENDED_KEY))
    if stamp is not None and dt.date.fromisoformat(stamp.value) <= start + dt.timedelta(days=31):
        return 0
    earliest = dict(session.execute(
        select(PriceHistory.ticker_id, func.min(PriceHistory.date))
        .group_by(PriceHistory.ticker_id)).tuples().all())
    tickers = session.scalars(select(Ticker).where(
        (Ticker.in_universe.is_(True) & (Ticker.asset_class == "stock"))
        | (Ticker.symbol == settings.benchmark_symbol))).all()
    due = [t for t in tickers
           if earliest.get(t.id) is None or earliest[t.id] > start + dt.timedelta(days=10)]
    fetched = 0
    for index, ticker in enumerate(due, start=1):
        if progress is not None:
            progress("extending price history (one-time)", index - 1, len(due), ticker.symbol)
        end = earliest[ticker.id] - dt.timedelta(days=1) if earliest.get(ticker.id) else today
        asset = "index" if ticker.symbol == settings.benchmark_symbol else "stock"
        try:
            ingest_history(session, provider, ticker.symbol, start, end, asset_class=asset)
            fetched += 1
        except Exception:  # one bad symbol must not sink a long one-time job
            pass
        session.commit()
    if stamp is None:
        stamp = AppSetting(key=HISTORY_EXTENDED_KEY, value=start.isoformat())
        session.add(stamp)
    stamp.value = start.isoformat()
    session.commit()
    return fetched


def run_lens_backtest(progress: Progress | None = None) -> dict:
    """Background entry point for the Screener's "Run backtest": extend
    history if it never was, replay the emerging lens over it, store it."""
    settings = get_settings()
    provider = build_provider(settings)
    with session_scope() as session:
        extended = extend_history(session, provider, settings, dt.date.today(), progress)
        result = run_backtest(session, benchmark_symbol=settings.benchmark_symbol,
                              progress=progress)
        result["history_extended"] = extended
        save_backtest(session, result)
        session.commit()
        return result


def run_nightly(
    session: Session, provider: MarketDataProvider, *,
    settings: Settings | None = None, today: dt.date | None = None,
    max_symbols: int | None = None, with_alternatives: bool = False,
    load_universe_if_empty: bool = False, refresh_universe: bool = False,
    progress: Progress | None = None,
) -> JobReport:
    """Full pipeline pass.  Safe to call any number of times per day."""
    settings = settings or get_settings()
    today = today or dt.date.today()
    report = JobReport()

    def step(phase: str, done: int | None = None, total: int | None = None,
             detail: str | None = None) -> None:
        if progress is not None:
            progress(phase, done, total, detail)

    # 0. the stock list: on a fresh install, and weekly (``refresh_universe``)
    has_universe = bool(session.scalar(
        select(func.count(Ticker.id)).where(Ticker.in_universe.is_(True))))
    if (load_universe_if_empty and not has_universe) or (
            refresh_universe and universe_due(session, settings)):
        step("loading the stock list")
        refresh_stock_list(session, settings, report, has_universe=has_universe)

    config = load_scan_config(session, settings)
    price_held = set(price_symbols(session, settings))

    # 1a. cheap history for every universe stock (skipped when already fresh)
    scan_symbols = scan_fetch_symbols(session, settings)
    if max_symbols:
        scan_symbols = scan_symbols[:max_symbols]
    since = today - dt.timedelta(days=SCAN_LOOKBACK_DAYS)
    for index, symbol in enumerate(scan_symbols, start=1):
        step("fetching prices", index - 1, len(scan_symbols), symbol)
        try:
            stats = ingest_history(session, provider, symbol, since, today)
            report.scan_fetched += 1
            report.notes.extend(f"{symbol}: {n}" for n in stats.notes if n.startswith("split "))
        except Exception:  # provider chain already retried; keep the job alive
            report.scan_fetch_errors += 1
        session.commit()

    # 1b. a rotating slice of fundamentals (stalest first)
    due = [s for s in fundamentals_due(session, config.fundamentals_per_night, today)
           if s not in price_held]
    if max_symbols:
        due = due[:max_symbols]
    for index, symbol in enumerate(due, start=1):
        step("fetching fundamentals", index - 1, len(due), symbol)
        try:
            if ingest_fundamentals(session, provider, symbol).written:
                report.fundamentals_refreshed += 1
        except Exception:
            report.scan_fetch_errors += 1
        session.commit()

    # 1b'. the market-wide earnings calendar (one call per day, whole market):
    #      surprises feed the emerging lens's post-earnings-drift signal.
    days = earnings_calendar_days(session, today)
    if max_symbols:
        days = days[-max_symbols:]
    for index, day in enumerate(days, start=1):
        step("fetching the earnings calendar", index - 1, len(days), day.isoformat())
        try:
            stats = ingest_earnings_calendar(session, provider, day)
            report.earnings_days += 1
            report.earnings_matched += stats.written
        except Exception:  # keep the job alive; the day is retried next run
            report.scan_fetch_errors += 1
        session.commit()

    # 1c. scan-score the whole universe -> shortlist
    step("scanning")
    scan: ScanResult = run_scan(session, today=today, shortlist_size=config.shortlist_size,
                                progress=progress)
    report.scan = scan.as_dict()
    session.commit()

    # 1d. the full fetch for the shortlist + everything held/watched
    symbols = target_symbols(session, settings)
    if max_symbols:
        symbols = symbols[:max_symbols]
    for index, symbol in enumerate(symbols, start=1):
        step("fetching shortlist details", index - 1, len(symbols), symbol)
        asset = "index" if symbol in (settings.benchmark_symbol, settings.vix_symbol) else "stock"
        try:
            outcomes = refresh_symbol(session, provider, symbol,
                                      with_alternatives=with_alternatives, asset_class=asset)
            report.fetched.append(symbol)
            report.notes.extend(f"{symbol}: {n}" for n in outcomes["history"].notes
                                if n.startswith("split "))
        except Exception as exc:  # provider chain already retries; keep the job alive
            report.fetch_errors.append(f"{symbol}: {exc}")
        session.commit()

    # 1e. rank the reviewed early movers by catalysts (their news/analyst rows
    #     just landed) - that order decides who gets AI time tonight
    step("weighing catalysts")
    confirm_emerging(session, today)
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

    # 2b. dividends whose ex-date has arrived (idempotent per account+dividend)
    report.dividends_booked = book_dividends(session, account, today)
    session.commit()

    # 3. pending limits, then mark + snapshot (so today's fills are in it)
    report.orders = process_pending_orders(session, account, settings=settings)
    session.commit()
    mark_positions(session, account)
    snapshot = write_snapshot(session, account, today, benchmark_symbol=settings.benchmark_symbol)
    report.snapshot_date = snapshot.snapshot_date.isoformat()
    session.commit()

    # 4. screen + grade
    screen = screen_universe(session, progress=progress, commit_each=True)
    report.scored = screen.scored
    report.new_recommendations = len(screen.new_recommendations)
    step("grading past calls")
    for rec in session.scalars(select(Recommendation)):
        if grade_recommendation(session, rec, benchmark_symbol=settings.benchmark_symbol,
                                today=today) is not None:
            report.graded += 1
    report.statuses = update_statuses(session, today=today)
    session.commit()

    # 5. alerts
    step("checking alerts")
    report.alerts_triggered = evaluate_alerts(session, settings, today=today)
    session.commit()
    return report


def refresh_stock_list(session: Session, settings: Settings, report: JobReport, *,
                       has_universe: bool) -> None:
    """Rebuild the universe from the US-listed scan (floors from Settings).

    Only a US-listed list (live or its cached CSV) may *replace* the universe;
    a degraded fallback (S&P 500, seed symbols) is used solely to bootstrap an
    empty database, so a flaky night can't shrink 3,000 stocks to 19.  Only a
    live fetch stamps the refresh, so a failed one is retried next run.
    """
    config = load_scan_config(session, settings)
    universe = load_universe(settings, min_market_cap=config.min_market_cap,
                             min_price=config.min_price,
                             min_dollar_volume=config.min_dollar_volume)
    report.notes.extend(universe.notes)
    us_listed = universe.source in ("nasdaq", "us_listed_csv")
    if us_listed or not has_universe:
        count = persist_universe(session, universe.members, replace=us_listed)
        report.notes.append(f"universe loaded: {count} tickers from {universe.source}")
        record_universe_refresh(session, universe.source, count,
                                stamp=universe.source == "nasdaq")
    else:
        report.notes.append(f"stock list kept: {universe.source} fallback not applied")
    session.commit()


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
