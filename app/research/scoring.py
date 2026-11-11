"""Transparent rule-based scoring: five sub-scores, weights, composite.

Every rule below is a plain linear map with an explicit threshold, documented
inline and pinned by ``tests/test_scoring.py``.  Two invariants enforced here:

* **Missing data is excluded, never defaulted.**  A sub-score whose components
  are all missing is ``None``; the composite renormalises the remaining
  weights.  A field rendered as 0 would read as "cheap/strong" and is the
  exact failure mode the spec forbids.
* **Weights live in the DB** (``scoring_weights``) so the settings page can
  retune the blend without a deploy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import ScoringWeight

ZERO = Decimal("0")
HUNDRED = Decimal("100")

#: name -> default weight; must sum to 1.0.
DEFAULT_WEIGHTS: dict[str, Decimal] = {
    "value": Decimal("0.30"),
    "momentum": Decimal("0.25"),
    "quality": Decimal("0.25"),
    "sentiment": Decimal("0.10"),
    "risk": Decimal("0.10"),
}


@dataclass(frozen=True)
class Evidence:
    """One cited data point: what it is, its value, where and when it came from."""

    label: str
    value: str
    source: str
    as_of: str | None = None
    url: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return {"label": self.label, "value": self.value, "source": self.source,
                "as_of": self.as_of, "url": self.url}


@dataclass
class Component:
    """One scored input inside a sub-score."""

    name: str
    score: Decimal | None
    note: str
    evidence: Evidence | None = None


@dataclass
class SubScore:
    """A category score plus its components; ``score`` None = fully unknown."""

    name: str
    score: Decimal | None
    components: list[Component] = field(default_factory=list)

    @property
    def missing(self) -> list[str]:
        return [c.name for c in self.components if c.score is None]

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "score": str(self.score) if self.score is not None else None,
            "components": [
                {"name": c.name,
                 "score": str(c.score) if c.score is not None else None,
                 "note": c.note}
                for c in self.components
            ],
        }


def _clamp(value: Decimal) -> Decimal:
    return max(ZERO, min(HUNDRED, value)).quantize(Decimal("0.01"))


def _map(value: Decimal | None, zero_at: Decimal, hundred_at: Decimal,
         evidence: Evidence | None = None, name: str = "") -> Component:
    """Linear map ``zero_at`` -> 0 points, ``hundred_at`` -> 100 (either direction)."""
    if value is None:
        return Component(name, None, "unavailable", None)
    span = hundred_at - zero_at
    if span == 0:  # pragma: no cover - guard against bad thresholds
        return Component(name, Decimal("50"), "degenerate threshold", evidence)
    score = (value - zero_at) / span * HUNDRED
    shown = value.quantize(Decimal("0.01"))
    note = f"{shown} mapped from [{zero_at}, {hundred_at}]"
    return Component(name, _clamp(score), note, evidence)


def _avg_known(components: list[Component]) -> Decimal | None:
    known = [c.score for c in components if c.score is not None]
    if not known:
        return None
    return (sum(known) / Decimal(len(known))).quantize(Decimal("0.01"))


# --------------------------------------------------------------------- value

def value_score(fund, sector_median_pe: Decimal | None,
                sector: str | None = None) -> SubScore:
    """Value: PE vs sector median, EV/EBITDA, P/S, P/B, FCF yield."""
    as_of = fund.as_of_date.isoformat() if fund and fund.as_of_date else None
    src = fund.source if fund else "unavailable"
    comps: list[Component] = []

    pe = fund.pe_trailing if fund else None
    if pe is not None and pe <= 0:
        comps.append(Component("pe_vs_sector", ZERO, f"negative PE ({pe}): not profitable",
                               Evidence("PE (trailing)", str(pe), src, as_of)))
    elif pe is not None and sector_median_pe is not None:
        # Sector median -> 50 points, half of it -> 100, double -> 0.
        comps.append(_map(pe, sector_median_pe * Decimal("2"), ZERO,
                          Evidence("PE vs sector median", f"{pe} vs {sector_median_pe}",
                                   src, as_of), "pe_vs_sector"))
    elif pe is not None:
        comps.append(_map(pe, Decimal("30"), Decimal("5"),
                          Evidence("PE (trailing)", str(pe), src, as_of), "pe_vs_sector"))
    else:
        comps.append(Component("pe_vs_sector", None, "unavailable", None))

    comps.append(_map(fund.ev_ebitda if fund else None, Decimal("25"), Decimal("8"),
                      Evidence("EV/EBITDA", str(fund.ev_ebitda), src, as_of)
                      if fund and fund.ev_ebitda is not None else None, "ev_ebitda"))
    comps.append(_map(fund.ps if fund else None, Decimal("12"), Decimal("0.5"),
                      Evidence("P/S", str(fund.ps), src, as_of)
                      if fund and fund.ps is not None else None, "ps"))
    comps.append(_map(fund.pb if fund else None, Decimal("8"), Decimal("0.5"),
                      Evidence("P/B", str(fund.pb), src, as_of)
                      if fund and fund.pb is not None else None, "pb"))
    comps.append(_map(fund.fcf_yield if fund else None, ZERO, Decimal("8"),
                      Evidence("FCF yield %", str(fund.fcf_yield), src, as_of)
                      if fund and fund.fcf_yield is not None else None, "fcf_yield"))
    return SubScore("value", _avg_known(comps), comps)


# ------------------------------------------------------------------ momentum

def momentum_score(returns: dict[str, Decimal | None], rsi_value: Decimal | None,
                   dist_from_high: Decimal | None, above_50: bool | None,
                   above_200: bool | None) -> SubScore:
    """Momentum: 1m/3m/6m/12m returns, 52-week-high distance, RSI, SMAs."""
    comps: list[Component] = []
    for label in ("1m", "3m", "6m", "12m"):
        value = returns.get(label)
        comps.append(_map(value, Decimal("-20"), Decimal("20"),
                          Evidence(f"{label} return %", str(value), "price_history")
                          if value is not None else None, f"return_{label}"))
    comps.append(_map(dist_from_high, Decimal("-50"), ZERO,
                      Evidence("distance from 52-wk high %", str(dist_from_high),
                               "price_history") if dist_from_high is not None else None,
                      "distance_from_high"))

    if rsi_value is None:
        comps.append(Component("rsi14", None, "unavailable", None))
    else:
        # Piecewise: oversold stays weak for momentum, 50-70 is the sweet spot,
        # at/above 80 is "overheated" and loses 2 points per RSI point over 70.
        if rsi_value >= 80:
            score = HUNDRED - (rsi_value - Decimal("70")) * Decimal("2")
            note = f"RSI {rsi_value} is overheated"
        else:
            score = rsi_value
            note = f"RSI {rsi_value} used directly"
        comps.append(Component("rsi14", _clamp(score), note,
                               Evidence("RSI(14)", str(rsi_value), "price_history")))

    if above_50 is None or above_200 is None:
        comps.append(Component("trend_vs_sma", None, "not enough history for 200-day SMA",
                               None))
    else:
        score = HUNDRED if (above_50 and above_200) else (
            Decimal("50") if (above_50 or above_200) else ZERO
        )
        comps.append(Component(
            "trend_vs_sma", score,
            f"above50={above_50} above200={above_200}",
            Evidence("vs 50/200-day SMA", f"{above_50}/{above_200}", "price_history"),
        ))
    return SubScore("momentum", _avg_known(comps), comps)


# ------------------------------------------------------------------- quality

def quality_score(fund) -> SubScore:
    """Quality: profit margin, ROE, debt/equity, revenue and EPS growth."""
    src = fund.source if fund else "unavailable"
    as_of = fund.as_of_date.isoformat() if fund and fund.as_of_date else None
    comps = [
        _map(fund.profit_margin if fund else None, ZERO, Decimal("20"),
             Evidence("profit margin %", str(fund.profit_margin), src, as_of)
             if fund and fund.profit_margin is not None else None, "profit_margin"),
        _map(fund.return_on_equity if fund else None, ZERO, Decimal("25"),
             Evidence("ROE %", str(fund.return_on_equity), src, as_of)
             if fund and fund.return_on_equity is not None else None, "return_on_equity"),
        _map(fund.debt_to_equity if fund else None, Decimal("2.5"), Decimal("0.3"),
             Evidence("debt/equity (ratio)", str(fund.debt_to_equity), src, as_of)
             if fund and fund.debt_to_equity is not None else None, "debt_to_equity"),
        _map(fund.revenue_growth_yoy if fund else None, Decimal("-5"), Decimal("15"),
             Evidence("revenue growth % YoY", str(fund.revenue_growth_yoy), src, as_of)
             if fund and fund.revenue_growth_yoy is not None else None, "revenue_growth"),
        _map(fund.eps_growth_yoy if fund else None, Decimal("-10"), Decimal("20"),
             Evidence("EPS growth % YoY", str(fund.eps_growth_yoy), src, as_of)
             if fund and fund.eps_growth_yoy is not None else None, "eps_growth"),
    ]
    return SubScore("quality", _avg_known(comps), comps)


# ----------------------------------------------------------------- sentiment

def sentiment_score(upgrades: int | None, downgrades: int | None,
                    target_upside_pct: Decimal | None,
                    news_avg_sentiment: Decimal | None) -> SubScore:
    """Sentiment: 90-day upgrade/downgrade counts, target distance, news tone."""
    comps: list[Component] = []
    if upgrades is None or downgrades is None:
        comps.append(Component("analyst_actions_90d", None, "unavailable", None))
    else:
        net = upgrades - downgrades
        score = Decimal("50") + Decimal(net) * Decimal("10")
        comps.append(Component(
            "analyst_actions_90d", _clamp(score),
            f"{upgrades} up/raise vs {downgrades} down/cut in 90d",
            Evidence("analyst actions 90d", f"+{upgrades}/-{downgrades}", "analyst_actions"),
        ))
    comps.append(_map(target_upside_pct, Decimal("-20"), Decimal("20"),
                      Evidence("upside to consensus target %", str(target_upside_pct),
                               "fundamentals") if target_upside_pct is not None else None,
                      "target_upside"))
    comps.append(_map(news_avg_sentiment, Decimal("-50"), Decimal("50"),
                      Evidence("avg news sentiment", str(news_avg_sentiment), "news_items")
                      if news_avg_sentiment is not None else None, "news_tone"))
    return SubScore("sentiment", _avg_known(comps), comps)


# ---------------------------------------------------------------------- risk

def risk_score(vol_pct: Decimal | None, drawdown_pct: Decimal | None,
               last_surprise_pct: Decimal | None,
               short_pct_float: Decimal | None) -> SubScore:
    """Risk (higher = safer): volatility, drawdown, earnings surprise, short interest."""
    comps = [
        _map(vol_pct, Decimal("45"), Decimal("15"),
             Evidence("annualised vol %", str(vol_pct), "price_history")
             if vol_pct is not None else None, "volatility"),
        _map(drawdown_pct, Decimal("-40"), ZERO,
             Evidence("max drawdown % (252d)", str(drawdown_pct), "price_history")
             if drawdown_pct is not None else None, "drawdown"),
        _map(last_surprise_pct, Decimal("-10"), Decimal("5"),
             Evidence("last EPS surprise %", str(last_surprise_pct), "earnings_events")
             if last_surprise_pct is not None else None, "earnings_surprise"),
        _map(short_pct_float, Decimal("15"), Decimal("2"),
             Evidence("short % float", str(short_pct_float), "fundamentals")
             if short_pct_float is not None else None, "short_interest"),
    ]
    return SubScore("risk", _avg_known(comps), comps)


# ------------------------------------------------------------------ weights

def ensure_default_weights(session: Session) -> None:
    """Seed the ``scoring_weights`` table once; never overwrites user edits."""
    existing = set(session.scalars(select(ScoringWeight.name)))
    for name, weight in DEFAULT_WEIGHTS.items():
        if name not in existing:
            session.add(ScoringWeight(name=name, weight=weight))
    session.flush()


def load_weights(session: Session) -> dict[str, Decimal]:
    """Current weights from the DB, falling back to defaults per name."""
    rows = {row.name: row.weight for row in session.scalars(select(ScoringWeight))}
    return {name: rows.get(name, default) for name, default in DEFAULT_WEIGHTS.items()}


def composite(sub_scores: list[SubScore], weights: dict[str, Decimal]) -> Decimal | None:
    """Weighted mean over *known* sub-scores; weights renormalise, never zero-fill.

    Returns ``None`` when no sub-score could be computed - an unrateable stock
    must not silently score 50/100.
    """
    total = ZERO
    weight_sum = ZERO
    for sub in sub_scores:
        if sub.score is None:
            continue
        w = weights.get(sub.name, ZERO)
        total += sub.score * w
        weight_sum += w
    if weight_sum == 0:
        return None
    return (total / weight_sum).quantize(Decimal("0.01"))
