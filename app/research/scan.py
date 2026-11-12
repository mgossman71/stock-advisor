"""Discovery scan: the cheap first stage of the screening funnel.

The universe is every US-listed common stock above the scan floors (~3,000
names), far too many to fetch news/analyst/earnings for, or to send through an
LLM, every night.  So the nightly pass runs as a funnel:

1. **scan** (this module): score every universe member from what is already
   stored - daily closes (one cheap history call per stock, skipped when
   fresh) plus the latest fundamentals snapshot (refreshed on a rotation,
   ``fundamentals_per_night`` stalest first).  Same sub-score functions and
   weights as the full report; sentiment can only see the consensus target
   here, and earnings surprise is unknown.  Nothing is fetched in here.
2. **shortlist**: the top ``shortlist_size`` scan scores get the full fetch
   (news, analyst actions, earnings) and a full report, through the LLM
   when one is configured - alongside everything held or watched.

Ranking puts names with fundamentals ahead of price-only names: a stock whose
fundamentals have not been fetched yet must not crowd out one that was scored
on the whole picture.  Missing inputs stay unknown (``composite`` renormalises
over the known blocks); a stock with too little history is not scored at all.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.background import Progress
from app.config import Settings, get_settings
from app.db.models import AppSetting, Fundamentals, PriceHistory, ScanScore, Ticker
from app.research import emerging
from app.research.emerging import Bars
from app.research.report import price_signals
from app.research.scoring import (
    composite, load_weights, momentum_score, quality_score, risk_score, sentiment_score,
    value_score,
)

D = Decimal
#: Same bar a full report needs: under ~3 months the momentum block is noise.
MIN_BARS_TO_SCAN = 60
#: ~13 months of bars: 12-month return + 200-day average, nothing more.
SCAN_LOOKBACK_DAYS = 400
#: Scan rows are a nightly working set, not history - keep two weeks.
KEEP_SCANS_DAYS = 14
#: A fundamentals snapshot younger than this is not re-fetched by the rotation.
FUNDAMENTALS_MIN_AGE_DAYS = 6

KEYS = {
    "min_market_cap": "scan_min_market_cap",
    "min_price": "scan_min_price",
    "min_dollar_volume": "scan_min_dollar_volume",
    "shortlist_size": "scan_shortlist_size",
    "emerging_size": "scan_emerging_size",
    "fundamentals_per_night": "scan_fundamentals_per_night",
}
UNIVERSE_SOURCE_KEY = "universe_source"
UNIVERSE_COUNT_KEY = "universe_count"
UNIVERSE_REFRESHED_KEY = "universe_refreshed_at"


# ------------------------------------------------------------------- config

@dataclass(frozen=True)
class ScanConfig:
    """Funnel knobs: Settings-page values over env defaults."""

    min_market_cap: Decimal
    min_price: Decimal
    min_dollar_volume: Decimal
    shortlist_size: int
    fundamentals_per_night: int
    #: Emerging-lens candidates fully researched each night (0 = off).
    emerging_size: int = 10


def _decimal(raw: str | None, default: Decimal) -> Decimal:
    try:
        value = Decimal(str(raw).replace(",", "").replace("$", "").strip())
    except (InvalidOperation, TypeError, ValueError):
        return default
    return value if value.is_finite() and value >= 0 else default


def _int(raw: str | None, default: int, low: int, high: int) -> int:
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def load_scan_config(session: Session, settings: Settings | None = None) -> ScanConfig:
    settings = settings or get_settings()
    rows = {s.key: s.value for s in session.scalars(
        select(AppSetting).where(AppSetting.key.in_(KEYS.values())))}
    return ScanConfig(
        min_market_cap=_decimal(rows.get(KEYS["min_market_cap"]), settings.scan_min_market_cap),
        min_price=_decimal(rows.get(KEYS["min_price"]), settings.scan_min_price),
        min_dollar_volume=_decimal(rows.get(KEYS["min_dollar_volume"]),
                                   settings.scan_min_dollar_volume),
        shortlist_size=_int(rows.get(KEYS["shortlist_size"]), settings.shortlist_size, 1, 500),
        fundamentals_per_night=_int(rows.get(KEYS["fundamentals_per_night"]),
                                    settings.scan_fundamentals_per_night, 0, 5000),
        emerging_size=_int(rows.get(KEYS["emerging_size"]),
                           settings.emerging_shortlist_size, 0, 200),
    )


def config_from_form(form, current: ScanConfig) -> ScanConfig:
    """Form fields -> config; a blank or garbage field keeps its current value.

    Market cap and dollar volume are entered in millions of dollars.
    """
    million = D("1000000")

    def millions(name: str, current_value: Decimal) -> Decimal:
        return _decimal(form.get(name), current_value / million) * million

    return ScanConfig(
        min_market_cap=millions("scan_min_market_cap_m", current.min_market_cap),
        min_price=_decimal(form.get("scan_min_price"), current.min_price),
        min_dollar_volume=millions("scan_min_dollar_volume_m", current.min_dollar_volume),
        shortlist_size=_int(form.get("scan_shortlist_size"), current.shortlist_size, 1, 500),
        fundamentals_per_night=_int(form.get("scan_fundamentals_per_night"),
                                    current.fundamentals_per_night, 0, 5000),
        emerging_size=_int(form.get("scan_emerging_size"), current.emerging_size, 0, 200),
    )


def _set(session: Session, key: str, value: str, now: dt.datetime) -> None:
    row = session.scalar(select(AppSetting).where(AppSetting.key == key))
    if row is None:
        row = AppSetting(key=key, value=value)
        session.add(row)
    row.value = value
    row.updated_at = now


def save_scan_config(session: Session, config: ScanConfig) -> bool:
    """Upsert every field (caller commits).  Returns True when a *floor*
    changed - the stock list then has to be rebuilt on the next full run."""
    before = load_scan_config(session)
    now = dt.datetime.now(dt.timezone.utc)
    for name, key in KEYS.items():
        _set(session, key, str(getattr(config, name)), now)
    floors_changed = (before.min_market_cap, before.min_price, before.min_dollar_volume) != (
        config.min_market_cap, config.min_price, config.min_dollar_volume)
    if floors_changed:
        # Clearing the refresh stamp makes ``universe_due`` true on the next run.
        session.execute(delete(AppSetting).where(AppSetting.key == UNIVERSE_REFRESHED_KEY))
    session.flush()
    return floors_changed


# ----------------------------------------------------------------- universe

@dataclass(frozen=True)
class UniverseInfo:
    source: str | None
    count: int | None
    refreshed_at: dt.datetime | None


def universe_info(session: Session) -> UniverseInfo:
    rows = {s.key: s.value for s in session.scalars(select(AppSetting).where(
        AppSetting.key.in_((UNIVERSE_SOURCE_KEY, UNIVERSE_COUNT_KEY, UNIVERSE_REFRESHED_KEY))))}
    refreshed = rows.get(UNIVERSE_REFRESHED_KEY)
    try:
        refreshed_at = dt.datetime.fromisoformat(refreshed) if refreshed else None
    except ValueError:
        refreshed_at = None
    count = rows.get(UNIVERSE_COUNT_KEY)
    return UniverseInfo(source=rows.get(UNIVERSE_SOURCE_KEY),
                        count=int(count) if count and count.isdigit() else None,
                        refreshed_at=refreshed_at)


def universe_due(session: Session, settings: Settings | None = None,
                 now: dt.datetime | None = None) -> bool:
    """True when the US-listed stock list is older than the refresh interval
    (or was never built from the live screener, or a floor changed)."""
    settings = settings or get_settings()
    now = now or dt.datetime.now(dt.timezone.utc)
    refreshed = universe_info(session).refreshed_at
    if refreshed is None:
        return True
    if refreshed.tzinfo is None:
        refreshed = refreshed.replace(tzinfo=dt.timezone.utc)
    return now - refreshed >= dt.timedelta(days=settings.universe_refresh_days)


def record_universe_refresh(session: Session, source: str, count: int, *,
                            stamp: bool = True, now: dt.datetime | None = None) -> None:
    """Remember where the stock list came from; ``stamp`` (live fetches only)
    starts the refresh interval, so a fallback is retried on the next run."""
    now = now or dt.datetime.now(dt.timezone.utc)
    _set(session, UNIVERSE_SOURCE_KEY, source, now)
    _set(session, UNIVERSE_COUNT_KEY, str(count), now)
    if stamp:
        _set(session, UNIVERSE_REFRESHED_KEY, now.isoformat(), now)
    session.flush()


# ------------------------------------------------------------- fetch plans

def universe_stock_symbols(session: Session) -> list[str]:
    """Every stock flagged ``in_universe`` (indices/ETFs excluded)."""
    return sorted(session.scalars(
        select(Ticker.symbol).where(Ticker.in_universe.is_(True),
                                    Ticker.asset_class == "stock")))


def fundamentals_due(session: Session, limit: int, today: dt.date) -> list[str]:
    """Universe stocks whose fundamentals are missing or stalest, up to ``limit``.

    Never-fetched names come first, then oldest ``as_of_date``; anything
    refreshed in the last ``FUNDAMENTALS_MIN_AGE_DAYS`` is left alone.
    """
    if limit <= 0:
        return []
    latest = (select(Fundamentals.ticker_id, func.max(Fundamentals.as_of_date).label("d"))
              .group_by(Fundamentals.ticker_id).subquery())
    cutoff = today - dt.timedelta(days=FUNDAMENTALS_MIN_AGE_DAYS)
    rows = session.execute(
        select(Ticker.symbol, latest.c.d)
        .outerjoin(latest, latest.c.ticker_id == Ticker.id)
        .where(Ticker.in_universe.is_(True), Ticker.asset_class == "stock",
               (latest.c.d.is_(None)) | (latest.c.d <= cutoff))
        .order_by(latest.c.d.is_not(None), latest.c.d, Ticker.market_cap.desc(), Ticker.symbol)
        .limit(limit)
    ).all()
    return [row.symbol for row in rows]


# --------------------------------------------------------------------- scan

@dataclass
class ScanResult:
    scan_date: dt.date
    universe: int = 0
    scored: int = 0
    with_fundamentals: int = 0
    skipped_short_history: int = 0
    shortlist: list[str] = field(default_factory=list)
    #: Emerging-lens candidates that get full research (see ``emerging_symbols``).
    emerging: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"scan_date": self.scan_date.isoformat(), "universe": self.universe,
                "scored": self.scored, "with_fundamentals": self.with_fundamentals,
                "skipped_short_history": self.skipped_short_history,
                "shortlist": self.shortlist, "emerging": self.emerging}


def _latest_fundamentals(session: Session) -> dict[int, Fundamentals]:
    latest = (select(Fundamentals.ticker_id, func.max(Fundamentals.as_of_date).label("d"))
              .group_by(Fundamentals.ticker_id).subquery())
    rows = session.scalars(
        select(Fundamentals).join(latest, (Fundamentals.ticker_id == latest.c.ticker_id)
                                  & (Fundamentals.as_of_date == latest.c.d)))
    return {row.ticker_id: row for row in rows}


def _median(values: list[Decimal]) -> Decimal | None:
    values = sorted(values)
    if not values:
        return None
    mid = len(values) // 2
    return values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / D("2")


def _bars(session: Session, ticker_id: int, since: dt.date) -> Bars:
    """Closes plus the high/low/volume the emerging lens needs, oldest first."""
    rows = session.execute(
        select(PriceHistory.adj_close, PriceHistory.close, PriceHistory.high,
               PriceHistory.low, PriceHistory.volume)
        .where(PriceHistory.ticker_id == ticker_id, PriceHistory.date >= since,
               PriceHistory.close.isnot(None))
        .order_by(PriceHistory.date)
    ).all()
    return Bars(closes=[r.adj_close if r.adj_close is not None else r.close for r in rows],
                raw_closes=[r.close for r in rows], highs=[r.high for r in rows],
                lows=[r.low for r in rows], volumes=[r.volume for r in rows])


def score_ticker(ticker: Ticker, closes: list[Decimal], fund: Fundamentals | None,
                 sector_median_pe: Decimal | None, weights: dict[str, Decimal]):
    """(composite, {block: score|None}, has_fundamentals) - or None if unscorable."""
    if len(closes) < MIN_BARS_TO_SCAN:
        return None
    signals = price_signals(closes)
    target = fund.target_price_consensus if fund else None
    upside = (target / closes[-1] - D("1")) * D("100") if target and closes[-1] else None
    subs = [
        value_score(fund, sector_median_pe, ticker.sector),
        momentum_score(signals.returns, signals.rsi, signals.dist_high,
                       signals.above50, signals.above200),
        quality_score(fund),
        sentiment_score(None, None, upside, None),
        risk_score(signals.vol, signals.drawdown, None,
                   fund.short_pct_float if fund else None),
    ]
    score = composite(subs, weights)
    if score is None:
        return None
    blocks = {s.name: s.score for s in subs}
    has_fundamentals = blocks.get("value") is not None or blocks.get("quality") is not None
    return score, blocks, has_fundamentals


def run_scan(session: Session, *, today: dt.date | None = None,
             shortlist_size: int | None = None,
             progress: Progress | None = None) -> ScanResult:
    """Score every universe stock from stored rows and rank them (idempotent:
    re-running on the same day replaces that day's rows)."""
    today = today or dt.date.today()
    size = shortlist_size or load_scan_config(session).shortlist_size
    result = ScanResult(scan_date=today)
    tickers = session.scalars(
        select(Ticker).where(Ticker.in_universe.is_(True), Ticker.asset_class == "stock")
        .order_by(Ticker.symbol)).all()
    result.universe = len(tickers)
    funds = _latest_fundamentals(session)
    sectors = dict(session.execute(select(Ticker.id, Ticker.sector)).tuples().all())
    by_sector: dict[str, list[Decimal]] = {}
    for ticker_id, fund in funds.items():
        sector = sectors.get(ticker_id)
        if sector and fund.pe_trailing is not None and fund.pe_trailing > 0:
            by_sector.setdefault(sector, []).append(fund.pe_trailing)
    sector_pe = {sector: _median(values) for sector, values in by_sector.items()}
    weights = load_weights(session)
    since = today - dt.timedelta(days=SCAN_LOOKBACK_DAYS)

    scored: list[tuple[Ticker, Decimal, dict, bool]] = []
    lens: dict[int, dict] = {}  # ticker_id -> emerging.signals(...)
    for index, ticker in enumerate(tickers, start=1):
        if progress is not None and (index == 1 or index % 50 == 0):
            progress("scanning", index - 1, len(tickers), ticker.symbol)
        bars = _bars(session, ticker.id, since)
        fund = funds.get(ticker.id)
        outcome = score_ticker(ticker, bars.closes, fund,
                               sector_pe.get(ticker.sector or ""), weights)
        if outcome is None:
            result.skipped_short_history += 1
            continue
        scored.append((ticker, *outcome))
        sig = emerging.signals(bars, revenue_growth=fund.revenue_growth_yoy if fund else None,
                               eps_growth=fund.eps_growth_yoy if fund else None,
                               market_cap=ticker.market_cap or (fund.market_cap if fund else None))
        if sig is not None:
            lens[ticker.id] = sig

    # Emerging lens: relative strength is a percentile of the whole scan, so
    # it can only be scored once every stock's signals are in.
    rs = emerging.percentiles({tid: s["rs_raw"] for tid, s in lens.items()
                               if s["rs_raw"] is not None})
    lens_scores: dict[int, tuple[Decimal, dict]] = {}
    for tid, sig in lens.items():
        points, reasons = emerging.score(sig, rs.get(tid))
        lens_scores[tid] = (points, emerging.stored(sig, rs.get(tid), reasons))
    lens_rank = {tid: rank for rank, tid in enumerate(
        sorted(lens_scores, key=lambda t: -lens_scores[t][0]), start=1)}

    scored.sort(key=lambda row: (not row[3], -row[1], row[0].symbol))
    session.execute(delete(ScanScore).where(
        (ScanScore.scan_date == today)
        | (ScanScore.scan_date < today - dt.timedelta(days=KEEP_SCANS_DAYS))))
    for rank, (ticker, score, blocks, has_fund) in enumerate(scored, start=1):
        points, detail = lens_scores.get(ticker.id, (None, None))
        session.add(ScanScore(
            ticker_id=ticker.id, scan_date=today, score=score, rank=rank,
            has_fundamentals=has_fund,
            sub_scores={k: (str(v) if v is not None else None) for k, v in blocks.items()},
            emerging_score=points, emerging_rank=lens_rank.get(ticker.id), emerging=detail,
        ))
    session.flush()
    result.scored = len(scored)
    result.with_fundamentals = sum(1 for row in scored if row[3])
    result.shortlist = [row[0].symbol for row in scored[:size]]
    emerging_size = load_scan_config(session).emerging_size
    by_id = {t.id: t.symbol for t, *_ in scored}
    result.emerging = [by_id[tid] for tid in sorted(lens_rank, key=lens_rank.get)
                       if lens_scores[tid][0] >= emerging.SHORTLIST_MIN_SCORE][:emerging_size]
    return result


def latest_scan_date(session: Session) -> dt.date | None:
    return session.scalar(select(func.max(ScanScore.scan_date)))


def shortlist_symbols(session: Session, size: int | None = None) -> list[str]:
    """Top ``size`` of the most recent scan (empty before the first scan)."""
    scan_date = latest_scan_date(session)
    if scan_date is None:
        return []
    size = size or load_scan_config(session).shortlist_size
    return list(session.scalars(
        select(Ticker.symbol).join(ScanScore, ScanScore.ticker_id == Ticker.id)
        .where(ScanScore.scan_date == scan_date, ScanScore.rank <= size)
        .order_by(ScanScore.rank)))


def emerging_symbols(session: Session, size: int | None = None) -> list[str]:
    """The emerging lens's shortlist from the latest scan: its top ``size``
    candidates scoring at least ``emerging.SHORTLIST_MIN_SCORE``."""
    scan_date = latest_scan_date(session)
    if scan_date is None:
        return []
    size = load_scan_config(session).emerging_size if size is None else size
    if size <= 0:
        return []
    return list(session.scalars(
        select(Ticker.symbol).join(ScanScore, ScanScore.ticker_id == Ticker.id)
        .where(ScanScore.scan_date == scan_date, ScanScore.emerging_rank.isnot(None),
               ScanScore.emerging_score >= emerging.SHORTLIST_MIN_SCORE)
        .order_by(ScanScore.emerging_rank).limit(size)))


def latest_emerging(session: Session, ticker_id: int) -> ScanScore | None:
    """This stock's row in the latest scan, if the lens scored it."""
    scan_date = latest_scan_date(session)
    if scan_date is None:
        return None
    return session.scalar(select(ScanScore).where(
        ScanScore.scan_date == scan_date, ScanScore.ticker_id == ticker_id,
        ScanScore.emerging_score.isnot(None)))


def emerging_candidates(session: Session, limit: int = 10) -> list[tuple[ScanScore, Ticker]]:
    """The latest scan's strongest emerging-lens stocks (score >= the
    shortlist bar), best first - the dashboard's "early movers" list."""
    scan_date = latest_scan_date(session)
    if scan_date is None:
        return []
    return list(session.execute(
        select(ScanScore, Ticker).join(Ticker, Ticker.id == ScanScore.ticker_id)
        .where(ScanScore.scan_date == scan_date, ScanScore.emerging_rank.isnot(None),
               ScanScore.emerging_score >= emerging.SHORTLIST_MIN_SCORE)
        .order_by(ScanScore.emerging_rank).limit(limit)).tuples())

