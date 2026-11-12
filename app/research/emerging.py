"""Emerging-opportunity lens: find stocks early in a big move, from price + volume.

The main scan ranks on the report composite (value, quality, momentum, risk),
which favours established, reasonably priced names.  A stock at the start of
a large run usually scores *badly* there - it looks expensive and volatile.
This lens looks for the footprints such runs tend to leave instead, using only
stored daily bars (and fundamentals where already fetched), so it costs no
network calls and no AI and can run over the whole ~3,000-stock universe.

Signals (each documented in the stock's ``reasons``):

* **Relative strength** - weighted 3/6/9/12-month return (3m counts double),
  as a percentile of the whole scanned universe.  Leaders keep leading.
* **Trend structure** - close > 50d > 150d > 200d average and the 200d rising.
* **Near / at highs** - within 5% of the 52-week high; breaking above the
  prior 20-day high on >= 1.5x average volume is a breakout.
* **Volume** - 10-day average volume vs the prior 50 days (fresh interest),
  and up-day vs down-day volume over 50 days (accumulation).
* **Tight base** - the 15 days before the last week trade in a narrow range
  versus the 60 before that (quiet consolidation, the classic set-up before
  a breakout; measured before the last week so a fresh breakout still counts).
* **Acceleration** - the last month is outpacing the 3-month rate.
* **Earnings drift** - an EPS beat of >= 10% in the last 30 days with the
  price still above its pre-report close (post-earnings-announcement drift,
  one of the best-documented effects in markets).  From the market-wide
  earnings calendar, so every stock gets it, not just the researched ones.
  A miss of <= -10% costs points; earnings due within 10 days are noted.
* **Growth** - revenue / EPS growth >= 25% year on year, when known.
* **Size tilt** - micro (< $300M), small (< $2B) and mid (< $10B) caps get a
  few points: the early, room-to-run names this lens is for (and the riskier
  ones - micro-cap calls are labelled high risk with confidence capped).

Penalties: more than 25% above the 50-day average (late - the move already
happened), and a close below the 200-day average (no uptrend).

Scores are points out of 100.  They are a *ranking* for "look here first",
not a forecast; the ``emerging-v1`` calls they lead to are graded by the same
scoreboard as everything else, so the lens has to prove itself.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

D = Decimal

MIN_BARS = 60
#: Minimum lens score for a stock to be shortlisted for full research.
SHORTLIST_MIN_SCORE = D("55")
#: Minimum lens score for an ``emerging-v1`` call (after full research).
CALL_MIN_SCORE = D("70")
#: Breakout trade levels: breakouts that fail tend to fail fast.
STOP_PCT = D("0.92")      # -8 %
TARGET_PCT = D("1.25")    # +25 %
ENGINE_VERSION = "emerging-v1"
#: Below this market cap a stock is a *micro cap*: it can run hardest and
#: falls hardest; every call on one is labelled and confidence is capped.
MICRO_CAP = D("3e8")
MICRO_CAP_MAX_CONFIDENCE = D("65")


def is_micro(market_cap: Decimal | None) -> bool:
    return market_cap is not None and market_cap < MICRO_CAP


@dataclass
class Bars:
    """Aligned daily series, oldest first.  ``closes`` are split/dividend
    adjusted (returns); ``raw_closes``/``highs``/``lows`` are as traded
    (pattern levels); a missing high/low/volume is ``None``."""

    closes: list[Decimal]
    raw_closes: list[Decimal]
    highs: list[Decimal | None]
    lows: list[Decimal | None]
    volumes: list[int | None]
    #: Bar dates (needed for the earnings-drift signal; optional elsewhere).
    dates: list[dt.date] | None = None


@dataclass(frozen=True)
class Earnings:
    """The earnings facts the lens uses: latest report and the next date."""

    reported_on: dt.date | None = None
    surprise_pct: Decimal | None = None
    next_on: dt.date | None = None


EARNINGS_FRESH_DAYS = 30
EARNINGS_SOON_DAYS = 10


def _ret(closes: list[Decimal], days: int) -> Decimal | None:
    if len(closes) <= days or not closes[-days - 1]:
        return None
    return (closes[-1] / closes[-days - 1] - D("1")) * D("100")


def _sma(values: list[Decimal], n: int, end: int = 0) -> Decimal | None:
    """Average of the ``n`` values ending ``end`` bars before the last."""
    window = values[len(values) - n - end:len(values) - end] if end else values[-n:]
    if len(values) < n + end or not window:
        return None
    return sum(window) / D(len(window))


def _avg(values: list) -> Decimal | None:
    known = [D(v) for v in values if v is not None]
    return sum(known) / D(len(known)) if known else None


def _pct(value: Decimal | None) -> str | None:
    return None if value is None else str(value.quantize(D("0.01")))


def signals(bars: Bars, *, revenue_growth: Decimal | None = None,
            eps_growth: Decimal | None = None,
            market_cap: Decimal | None = None,
            earnings: Earnings | None = None) -> dict | None:
    """Raw metrics for one stock, or ``None`` with too little history.

    ``rs_raw`` is the input to the universe-wide percentile; everything else
    is self-contained.  Values are Decimals or bools; ``None`` = unknown.
    """
    closes, raw = bars.closes, bars.raw_closes
    if len(closes) < MIN_BARS or not raw[-1]:
        return None
    last = raw[-1]
    r = {label: _ret(closes, days)
         for label, days in (("1m", 21), ("3m", 63), ("6m", 126), ("9m", 189), ("12m", 252))}
    weighted = [(r["3m"], D("2")), (r["6m"], D("1")), (r["9m"], D("1")), (r["12m"], D("1"))]
    known = [(v, w) for v, w in weighted if v is not None]
    rs_raw = sum(v * w for v, w in known) / sum(w for _, w in known) if known else None

    sma50, sma150, sma200 = _sma(raw, 50), _sma(raw, 150), _sma(raw, 200)
    sma200_month_ago = _sma(raw, 200, end=21)
    trend = None
    if sma200 is not None and sma150 is not None and sma50 is not None:
        trend = (last > sma50 > sma150 > sma200
                 and sma200_month_ago is not None and sma200 > sma200_month_ago)

    year_highs = [h for h in bars.highs[-252:] if h is not None] or raw[-252:]
    high_52w = max(year_highs)
    dist_high = (last / high_52w - D("1")) * D("100")
    prior_20_highs = [h for h in bars.highs[-21:-1] if h is not None] or raw[-21:-1]

    vol_50 = _avg(bars.volumes[-60:-10])
    vol_10 = _avg(bars.volumes[-10:])
    vol_ratio = vol_10 / vol_50 if vol_10 is not None and vol_50 else None
    today_vol = bars.volumes[-1]
    breakout = bool(prior_20_highs) and last > max(prior_20_highs) and (
        today_vol is not None and vol_50 is not None and D(today_vol) >= vol_50 * D("1.5"))

    up_vol = down_vol = D("0")
    for i in range(max(1, len(raw) - 50), len(raw)):
        v = bars.volumes[i]
        if v is None:
            continue
        if raw[i] > raw[i - 1]:
            up_vol += v
        elif raw[i] < raw[i - 1]:
            down_vol += v
    updown = up_vol / down_vol if down_vol else None

    def span(lo: int, hi: int) -> Decimal | None:
        highs = [h for h in bars.highs[lo:hi] if h is not None]
        lows = [x for x in bars.lows[lo:hi] if x is not None]
        if not highs or not lows or not raw[hi - 1]:
            return None
        return (max(highs) - min(lows)) / raw[hi - 1]

    # The base is measured *before* the last week, so a stock that has just
    # broken out of a tight range still gets credit for the range.
    n = len(raw)
    recent, before = span(n - 20, n - 5), span(n - 80, n - 20) if n >= 80 else None
    tightness = recent / before if recent is not None and before else None

    # Earnings drift: judged against the last close *before* the report, so
    # "held the gains" means the market kept what the surprise gave it.
    as_of = bars.dates[-1] if bars.dates else None
    surprise = days_since = held = days_to_next = None
    if earnings is not None and as_of is not None:
        if (earnings.reported_on is not None and earnings.surprise_pct is not None
                and 0 <= (as_of - earnings.reported_on).days <= EARNINGS_FRESH_DAYS):
            surprise = earnings.surprise_pct
            days_since = (as_of - earnings.reported_on).days
            before = [i for i, d in enumerate(bars.dates) if d < earnings.reported_on]
            if before:
                held = last > raw[before[-1]]
        if earnings.next_on is not None and 0 <= (earnings.next_on - as_of).days \
                <= EARNINGS_SOON_DAYS:
            days_to_next = (earnings.next_on - as_of).days

    accel = (r["1m"] is not None and r["3m"] is not None
             and r["1m"] > 0 and r["1m"] * 3 > r["3m"])
    extension = (last / sma50 - D("1")) * D("100") if sma50 else None

    return {
        "rs_raw": rs_raw, "returns": r, "trend": trend, "dist_high": dist_high,
        "breakout": breakout, "vol_ratio": vol_ratio, "updown": updown,
        "tightness": tightness, "accel": accel, "extension": extension,
        "above200": None if sma200 is None else last > sma200,
        "revenue_growth": revenue_growth, "eps_growth": eps_growth,
        "market_cap": market_cap,
        "surprise": surprise, "days_since_report": days_since, "held_gains": held,
        "days_to_earnings": days_to_next,
    }


def score(sig: dict, rs_percentile: Decimal | None) -> tuple[Decimal, list[str]]:
    """Points out of 100 plus the plain-English reasons that earned them."""
    points = D("0")
    reasons: list[str] = []

    def add(n: str, why: str) -> None:
        nonlocal points
        points += D(n)
        reasons.append(why)

    if rs_percentile is not None:
        points += rs_percentile * D("0.30")  # up to 30
        if rs_percentile >= 80:
            reasons.append(f"relative strength in the top {100 - int(rs_percentile)}% "
                           f"of the market")
    if sig["trend"]:
        add("15", "uptrend: price > 50d > 150d > 200d average, 200d rising")
    dist = sig["dist_high"]
    if dist >= D("-5"):
        add("10", f"{abs(dist).quantize(D('0.1'))}% from its 52-week high")
    elif dist >= D("-15"):
        add("5", f"{abs(dist).quantize(D('0.1'))}% below its 52-week high")
    if sig["breakout"]:
        add("10", "broke above its 20-day high on heavy volume")
    vr = sig["vol_ratio"]
    if vr is not None and vr >= D("2"):
        add("8", f"volume {vr.quantize(D('0.1'))}x its recent average")
    elif vr is not None and vr >= D("1.5"):
        add("5", f"volume {vr.quantize(D('0.1'))}x its recent average")
    ud = sig["updown"]
    if ud is not None and ud >= D("1.6"):
        add("10", f"accumulation: {ud.quantize(D('0.1'))}x more volume on up days")
    elif ud is not None and ud >= D("1.3"):
        add("6", f"accumulation: {ud.quantize(D('0.1'))}x more volume on up days")
    if sig["tightness"] is not None and sig["tightness"] <= D("0.5"):
        add("5", "tight consolidation before the latest move")
    if sig["accel"]:
        add("5", f"momentum accelerating: +{sig['returns']['1m'].quantize(D('0.1'))}% "
                 f"in the last month")
    surprise = sig.get("surprise")
    if surprise is not None:
        when = f"{sig['days_since_report']} days ago"
        if surprise >= D("10") and sig.get("held_gains"):
            add("10", f"beat EPS estimates by {surprise.quantize(D('1'))}% {when} "
                      f"and held the gains")
        elif surprise >= D("10"):
            reasons.append(f"beat EPS estimates by {surprise.quantize(D('1'))}% {when}, "
                           f"but the price gave the gains back")
        elif surprise <= D("-10"):
            points -= D("5")
            reasons.append(f"caution: missed EPS estimates by "
                           f"{abs(surprise).quantize(D('1'))}% {when}")
    if sig.get("days_to_earnings") is not None:
        reasons.append(f"note: earnings due in {sig['days_to_earnings']} days (event risk)")
    for key, label in (("revenue_growth", "revenue"), ("eps_growth", "EPS")):
        value = sig[key]
        if value is not None and value >= D("25"):
            # Above ~200% it is usually a base effect (earnings up from near
            # zero), so say that instead of quoting a misleading number.
            add("5", f"{label} growing {value.quantize(D('1'))}% a year" if value <= D("200")
                else f"{label} growth over 200% a year (from a low base)")

    cap = sig.get("market_cap")
    if is_micro(cap):
        add("5", "micro cap: high risk, thin trading")
    elif cap is not None and cap < D("2e9"):
        add("5", "small cap: more room to run (and more risk)")
    elif cap is not None and cap < D("10e9"):
        add("3", "mid cap: room to run")

    ext = sig["extension"]
    if ext is not None and ext > D("25"):
        points -= D("10")
        reasons.append(f"caution: {ext.quantize(D('1'))}% above its 50-day average "
                       f"(may be late)")
    if sig["above200"] is False:
        points -= D("10")
        reasons.append("caution: below its 200-day average")
    return max(D("0"), min(D("100"), points)).quantize(D("0.01")), reasons


def catalyst_points(upgrades: int, downgrades: int,
                    news_sentiment: Decimal | None) -> tuple[Decimal, list[str]]:
    """Second-stage points from the full fetch (no AI): analyst actions in the
    last 30 days (upgrades + target raises vs downgrades + cuts) and mean
    news sentiment.  Ranks the reviewed candidates for AI time."""
    points = D("0")
    reasons: list[str] = []
    net = upgrades - downgrades
    if net:
        points += max(D("-12"), min(D("12"), D(net) * D("4")))
        reasons.append(f"analysts: {upgrades} upgrade(s)/target raise(s), "
                       f"{downgrades} downgrade(s)/cut(s) in 30 days")
    if news_sentiment is not None and news_sentiment >= D("15"):
        points += D("4")
        reasons.append(f"news tone positive ({news_sentiment.quantize(D('1'))})")
    elif news_sentiment is not None and news_sentiment <= D("-15"):
        points -= D("4")
        reasons.append(f"news tone negative ({news_sentiment.quantize(D('1'))})")
    return points, reasons


def percentiles(values: dict[int, Decimal]) -> dict[int, Decimal]:
    """key -> percentile rank 0..100 of its value (ties share the lower rank)."""
    ordered = sorted(values.items(), key=lambda kv: kv[1])
    n = len(ordered)
    if n == 1:
        return {ordered[0][0]: D("100")}
    out: dict[int, Decimal] = {}
    rank = 0
    for i, (key, value) in enumerate(ordered):
        if i and value != ordered[i - 1][1]:
            rank = i
        out[key] = (D(rank) * D("100") / D(n - 1)).quantize(D("0.1"))
    return out


def stored(sig: dict, rs_percentile: Decimal | None, reasons: list[str]) -> dict:
    """JSON-safe snapshot kept on the scan row (and handed to the AI analyst)."""
    return {
        "reasons": reasons,
        "rs_percentile": _pct(rs_percentile),
        "returns_pct": {k: _pct(v) for k, v in sig["returns"].items()},
        "distance_from_52w_high_pct": _pct(sig["dist_high"]),
        "uptrend": sig["trend"], "breakout": sig["breakout"],
        "volume_vs_avg": _pct(sig["vol_ratio"]), "up_down_volume": _pct(sig["updown"]),
        "base_tightness": _pct(sig["tightness"]),
        "pct_above_50d": _pct(sig["extension"]),
        "revenue_growth_pct": _pct(sig["revenue_growth"]),
        "eps_growth_pct": _pct(sig["eps_growth"]),
        "market_cap": str(sig["market_cap"]) if sig.get("market_cap") is not None else None,
        "eps_surprise_pct": _pct(sig.get("surprise")),
        "days_since_report": sig.get("days_since_report"),
        "held_post_earnings_gains": sig.get("held_gains"),
        "days_to_earnings": sig.get("days_to_earnings"),
    }


def call_levels(entry: Decimal) -> tuple[Decimal, Decimal]:
    """(stop, target) for an emerging-v1 long."""
    return entry * STOP_PCT, entry * TARGET_PCT


def call_confidence(lens_score: Decimal) -> Decimal:
    """Lens 70 -> 55 ... lens 100 -> 85 confidence."""
    return max(D("10"), min(D("85"), D("55") + (lens_score - CALL_MIN_SCORE))).quantize(D("0.1"))
