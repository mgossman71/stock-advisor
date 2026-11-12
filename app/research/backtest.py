"""Walk-forward backtest of the emerging lens: does it pick winners?

At each month-end in stored history, score every universe stock with the
*same* ``emerging.signals`` / ``score`` / ``percentiles`` the nightly scan
uses, from bars up to that date only (the 400-day scan window, no look-ahead),
then measure what happened next:

* forward 21- and 63-trading-day returns, and the excess over the benchmark
  for the same window;
* for the -8 % / +25 % levels of an ``emerging-v1`` call, which was hit first
  within 63 days (target, stop, or neither).

Grouped as: the top N picks per date, score buckets (55-70, 70-85, 85+),
and every scored stock (the baseline any edge has to beat).

What it cannot do, stated in the result: the universe is today's listings
(survivorship bias), there are no trading costs, the earnings-drift and
growth signals are left out (that history isn't stored), and size uses
today's market cap.  It is evidence about the price/volume core, not proof.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.background import Progress
from app.db.models import AppSetting, PriceHistory, Ticker
from app.research import emerging
from app.research.emerging import Bars

D = Decimal
SETTING_KEY = "lens_backtest"
HORIZONS = (21, 63)
#: Same window the live scan uses, so a backtest score = the score it would
#: have shown that night.
WINDOW_DAYS = 400
BUCKETS = (("55–70", D("55"), D("70")), ("70–85", D("70"), D("85")), ("85+", D("85"), None))
CAVEATS = (
    "Universe is today's listings: companies that failed or were delisted are missing "
    "(survivorship bias flatters every group, including the baseline).",
    "No trading costs or slippage.",
    "Earnings-drift and growth signals are not in the backtest (that history isn't "
    "stored); size uses today's market cap.",
    "Past results don't guarantee future ones.",
)


@dataclass
class _Series:
    dates: list[dt.date]
    adj: list[Decimal]
    raw: list[Decimal]
    highs: list[Decimal | None]
    lows: list[Decimal | None]
    volumes: list[int | None]


def _load(session: Session, ticker_id: int) -> _Series:
    rows = session.execute(
        select(PriceHistory.date, PriceHistory.adj_close, PriceHistory.close,
               PriceHistory.high, PriceHistory.low, PriceHistory.volume)
        .where(PriceHistory.ticker_id == ticker_id, PriceHistory.close.isnot(None),
               PriceHistory.provisional.is_(False))
        .order_by(PriceHistory.date)).all()
    return _Series(dates=[r.date for r in rows],
                   adj=[r.adj_close if r.adj_close is not None else r.close for r in rows],
                   raw=[r.close for r in rows], highs=[r.high for r in rows],
                   lows=[r.low for r in rows], volumes=[r.volume for r in rows])


def _index_on_or_before(dates: list[dt.date], day: dt.date) -> int | None:
    lo, hi = 0, len(dates) - 1
    found = None
    while lo <= hi:
        mid = (lo + hi) // 2
        if dates[mid] <= day:
            found, lo = mid, mid + 1
        else:
            hi = mid - 1
    return found


def month_ends(dates: list[dt.date], months: int, min_forward: int) -> list[dt.date]:
    """Last trading date of each month, newest ``months`` with at least
    ``min_forward`` bars after them (so every pick can be graded)."""
    ends: list[int] = []
    for i, day in enumerate(dates):
        if i + 1 == len(dates) or dates[i + 1].month != day.month:
            ends.append(i)
    graded = [dates[i] for i in ends if i + min_forward < len(dates)]
    return graded[-months:]


def _forward(series: _Series, i: int) -> dict:
    """What happened after bar ``i``: returns per horizon, and whether the
    emerging-v1 stop or target was touched first within the longest one."""
    out: dict = {}
    for h in HORIZONS:
        if i + h < len(series.adj) and series.adj[i]:
            out[h] = (series.adj[i + h] / series.adj[i] - 1) * 100
    entry = series.raw[i]
    stop, target = emerging.call_levels(entry)
    first = "neither"
    for j in range(i + 1, min(i + 1 + max(HORIZONS), len(series.raw))):
        low = series.lows[j] if series.lows[j] is not None else series.raw[j]
        high = series.highs[j] if series.highs[j] is not None else series.raw[j]
        if low <= stop:  # same-bar ambiguity resolves to the stop (conservative)
            first = "stop"
            break
        if high >= target:
            first = "target"
            break
    out["first"] = first
    return out


def run_backtest(session: Session, *, benchmark_symbol: str, months: int = 24,
                 top_n: int = 10, progress: Progress | None = None) -> dict:
    """Score every universe stock at each month-end and grade the outcomes."""
    bench_ticker = session.scalar(select(Ticker).where(Ticker.symbol == benchmark_symbol))
    if bench_ticker is None:
        return {"error": f"no {benchmark_symbol} history stored"}
    bench = _load(session, bench_ticker.id)
    dates = month_ends(bench.dates, months, min_forward=min(HORIZONS))
    if not dates:
        return {"error": "not enough benchmark history for a backtest"}
    bench_fwd = {}
    for day in dates:
        i = _index_on_or_before(bench.dates, day)
        bench_fwd[day] = _forward(bench, i) if i is not None else {}

    tickers = session.scalars(
        select(Ticker).where(Ticker.in_universe.is_(True), Ticker.asset_class == "stock")
        .order_by(Ticker.symbol)).all()
    # Per (date, ticker): signals + forward outcome.  Bars are loaded once per
    # ticker and sliced per date, so memory stays one ticker deep.
    per_date: dict[dt.date, dict[int, tuple[dict, dict, str]]] = {d: {} for d in dates}
    for index, ticker in enumerate(tickers, start=1):
        if progress is not None and (index == 1 or index % 25 == 0):
            progress("backtesting", index - 1, len(tickers), ticker.symbol)
        series = _load(session, ticker.id)
        if len(series.dates) < emerging.MIN_BARS:
            continue
        for day in dates:
            i = _index_on_or_before(series.dates, day)
            if i is None or (day - series.dates[i]).days > 5:
                continue  # not trading around that date
            start = _index_on_or_before(series.dates, day - dt.timedelta(days=WINDOW_DAYS))
            lo = 0 if start is None else start + 1
            window = Bars(closes=series.adj[lo:i + 1], raw_closes=series.raw[lo:i + 1],
                          highs=series.highs[lo:i + 1], lows=series.lows[lo:i + 1],
                          volumes=series.volumes[lo:i + 1], dates=series.dates[lo:i + 1])
            sig = emerging.signals(window, market_cap=ticker.market_cap)
            if sig is None:
                continue
            per_date[day][ticker.id] = (sig, _forward(series, i), ticker.symbol)

    groups: dict[str, list[dict]] = {"top": [], "all": []}
    groups.update({label: [] for label, _, _ in BUCKETS})
    picks_by_date: list[dict] = []
    for day in dates:
        rows = per_date[day]
        pct = emerging.percentiles({tid: s["rs_raw"] for tid, (s, _, _) in rows.items()
                                    if s["rs_raw"] is not None})
        scored = []
        for tid, (sig, fwd, symbol) in rows.items():
            points, _ = emerging.score(sig, pct.get(tid))
            outcome = {"symbol": symbol, "score": points, "first": fwd["first"]}
            for h in HORIZONS:
                if h in fwd:
                    outcome[f"ret{h}"] = fwd[h]
                    if h in bench_fwd[day]:
                        outcome[f"excess{h}"] = fwd[h] - bench_fwd[day][h]
            scored.append(outcome)
        scored.sort(key=lambda o: (-o["score"], o["symbol"]))
        groups["all"].extend(scored)
        groups["top"].extend(scored[:top_n])
        for label, low, high in BUCKETS:
            groups[label].extend(o for o in scored
                                 if o["score"] >= low and (high is None or o["score"] < high))
        picks_by_date.append({"date": day.isoformat(), "universe": len(scored),
                              "top": [_jsonable(o) for o in scored[:top_n]]})

    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "benchmark": benchmark_symbol, "months": len(dates), "top_n": top_n,
        "first_date": dates[0].isoformat(), "last_date": dates[-1].isoformat(),
        "groups": {name: _summary(rows) for name, rows in groups.items()},
        "by_date": picks_by_date,
        "caveats": list(CAVEATS),
    }


def _avg(values: list[Decimal]) -> str | None:
    return str((sum(values) / D(len(values))).quantize(D("0.01"))) if values else None


def _summary(rows: list[dict]) -> dict:
    out: dict = {"n": len(rows)}
    for h in HORIZONS:
        rets = [o[f"ret{h}"] for o in rows if f"ret{h}" in o]
        excess = [o[f"excess{h}"] for o in rows if f"excess{h}" in o]
        out[f"avg_return_{h}d"] = _avg(rets)
        out[f"avg_excess_{h}d"] = _avg(excess)
        out[f"beat_benchmark_{h}d_pct"] = (
            str((D(sum(e > 0 for e in excess)) * 100 / D(len(excess))).quantize(D("0.1")))
            if excess else None)
    for outcome in ("target", "stop", "neither"):
        out[f"{outcome}_first_pct"] = (
            str((D(sum(o["first"] == outcome for o in rows)) * 100 / D(len(rows)))
                .quantize(D("0.1"))) if rows else None)
    return out


def _jsonable(outcome: dict) -> dict:
    return {k: (str(v.quantize(D("0.01"))) if isinstance(v, Decimal) else v)
            for k, v in outcome.items()}


def save_result(session: Session, result: dict) -> None:
    row = session.scalar(select(AppSetting).where(AppSetting.key == SETTING_KEY))
    if row is None:
        row = AppSetting(key=SETTING_KEY, value="")
        session.add(row)
    row.value = json.dumps(result)
    row.updated_at = dt.datetime.now(dt.timezone.utc)


def load_result(session: Session) -> dict | None:
    raw = session.scalar(select(AppSetting.value).where(AppSetting.key == SETTING_KEY))
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None
