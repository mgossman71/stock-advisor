"""``LLMAdvisor``: a self-hosted model does the analysis, the DB supplies the facts.

The flow per ticker:

1. ``build_report(persist=False)`` computes the rules baseline exactly as
   ``RuleBasedAdvisor`` would (indicators, sub-scores, cited evidence).
2. A dossier of *stored* rows - that baseline plus fundamentals, a price
   path, recent news, analyst actions and earnings - goes to the model with
   instructions to use nothing else.
3. The reply is validated field by field.  Valid -> the report carries the
   model's stance/score/summary/points, with the rules view kept under
   ``payload["rules"]``.  Invalid or unreachable -> the rules report is
   persisted unchanged with ``payload["llm_error"]`` saying why.

Guards (pinned by ``tests/test_llm_advisor.py``):

* The entry price is always the latest stored close - a price the model
  writes is never used, so a call can't be anchored on a hallucination.
* Stops/targets on the wrong side of entry, or absurdly far from it, are
  replaced by the rules engine's defaults and the rationale says so.
* A call that contradicts the model's own stance, or has confidence below
  ``MIN_CONFIDENCE``, is not emitted.
* After a transport failure the advisor stops calling the model for the rest
  of its life (one screen run), so a down server costs one timeout, not 100.
"""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    AnalystAction, EarningsEvent, Fundamentals, NewsItem, Recommendation, ResearchReport,
    Ticker,
)
from app.db.types import PCT
from app.research import emerging
from app.research.engine import AdvisorEngine, RuleBasedAdvisor
from app.research.llm import (
    LLMClient, LLMConfig, LLMError, LLMUnreachable, extract_json, load_llm_config,
)
from app.research.report import build_report, closes_with_dates
from app.research.scan import latest_emerging
from app.trading.orders import latest_price

D = Decimal

STANCES = ("bullish", "neutral", "bearish")
ACTIONS = ("long", "short", "none")
HORIZONS = ("short", "medium", "long")
MAX_POINTS = 8
MAX_POINT_CHARS = 400

SYSTEM_PROMPT = """\
You are a skeptical, disciplined equity research analyst. You evaluate one \
US-listed stock using ONLY the JSON data in the user's message.

Rules:
- null or absent means UNKNOWN. Never treat it as zero and never guess it.
- Do not invent numbers, prices, news, events or facts that are not in the data.
- Every bull/bear point and risk must cite at least one specific figure or \
headline from the data.
- Fields ending in _pct are percent points (12.5 means 12.5%). debt_to_equity \
is a plain ratio. Prices are in USD.
- rules_baseline is a simple threshold model. Use it as one input; you may \
disagree, but say why in the summary.
- Weigh valuation against growth and quality, trend and momentum against \
mean-reversion risk, and analyst/news flow against what is already priced in.
- If emerging_signals is present, a breakout/momentum screen flagged this \
stock as a possible early-stage move. Judge whether the news, earnings and \
analyst actions show a real catalyst behind it (new product, earnings \
acceleration, upgrades, sector tailwind) or whether it looks like noise, a \
one-off spike or a move that is already late. A strong, early move with a \
real catalyst can justify bullish even when valuation looks stretched.
- Recommend "none" unless the evidence gives a clear, specific edge over the \
next 1-6 months. Most stocks on most days deserve "none".
- "long" requires stance bullish; "short" requires stance bearish.
- target_price and stop_price are absolute USD levels relative to last_close: \
for long, stop < last_close < target; for short, target < last_close < stop.

Reply with ONE JSON object and nothing else, exactly this shape:
{
  "stance": "bullish" | "neutral" | "bearish",
  "score": <number 0-100, 50 = neutral, higher = more attractive to own>,
  "summary": "<2-4 sentences: the thesis and the deciding factors>",
  "bull_points": ["<point citing data>", ...],
  "bear_points": ["<point citing data>", ...],
  "risks": ["<specific risk citing data>", ...],
  "recommendation": {
    "action": "long" | "short" | "none",
    "confidence": <number 0-100>,
    "horizon": "short" | "medium" | "long",
    "target_price": <number or null>,
    "stop_price": <number or null>,
    "rationale": "<1-3 sentences>"
  }
}"""


class LLMAdvisor:
    """AdvisorEngine backed by an OpenAI-compatible, usually self-hosted, model."""

    MIN_CONFIDENCE = D("55")

    def __init__(self, config: LLMConfig, *, client: LLMClient | None = None):
        self.config = config
        self.version = f"llm:{config.model}"[:60]
        self._client = client
        self._rules = RuleBasedAdvisor()
        self._unreachable: str | None = None

    # ------------------------------------------------------------ protocol

    def research(self, session: Session, symbol: str) -> ResearchReport | None:
        report = build_report(session, symbol, persist=False)
        if report is None:
            return None
        if self._unreachable is not None:
            error = f"skipped: model unreachable earlier in this run ({self._unreachable})"
        else:
            try:
                analysis = self.analyse(session, report)
                error = None
            except LLMUnreachable as exc:
                self._unreachable = str(exc)[:200]
                error = str(exc)
            except LLMError as exc:
                error = str(exc)
        if error is None:
            _apply(report, analysis, self.version)
        else:
            report.payload_json = {**(report.payload_json or {}), "llm_error": error[:500]}
        session.add(report)
        session.flush()
        return report

    def recommendations_for(self, session: Session,
                            report: ResearchReport) -> list[Recommendation]:
        llm = (report.payload_json or {}).get("llm")
        if not llm:
            # The model failed for this report, so it is a rules report:
            # the rules engine (and its version stamp) decides.
            return self._rules.recommendations_for(session, report)
        call = llm.get("recommendation") or {}
        action = call.get("action")
        if action not in ("long", "short"):
            return []
        confidence = _dec(call.get("confidence"))
        if confidence is None or confidence < self.MIN_CONFIDENCE:
            return []
        priced = latest_price(session, report.ticker_id)
        if priced is None:
            return []  # no stored price -> no call (never invent an entry)
        entry, _as_of = priced
        stop, target, adjusted = _levels(action, entry, _dec(call.get("stop_price")),
                                         _dec(call.get("target_price")))
        rationale = call.get("rationale") or report.summary
        if adjusted:
            rationale += f" [{adjusted}]"
        rec = self._rules._make(
            session, report, action, entry,
            confidence=min(D("95"), confidence).quantize(D("0.1")),
            stop=stop, target=target, rationale=rationale,
            horizon=call.get("horizon") or "medium",
            engine_version=report.model_or_rule_version,
        )
        return [rec] if rec is not None else []

    # -------------------------------------------------------------- model

    def analyse(self, session: Session, report: ResearchReport) -> dict:
        """Ask the model about ``report`` and return the validated analysis."""
        dossier = build_dossier(session, report)
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(dossier, default=str, indent=1)},
        ]
        client = self._client or LLMClient(self.config)
        try:
            reply = client.chat(messages)
        finally:
            if self._client is None:
                client.close()
        return validate_analysis(extract_json(reply))


def advisor_for(session: Session, *, screening: bool = False) -> AdvisorEngine:
    """The engine the user configured on the Settings page.

    ``screening=True`` is the nightly universe pass, which can opt out of the
    model (one call per ticker is slow on local hardware).
    """
    config = load_llm_config(session)
    if config.ready and (config.use_in_screen or not screening):
        return LLMAdvisor(config)
    return RuleBasedAdvisor()


# ----------------------------------------------------------------- dossier

def build_dossier(session: Session, report: ResearchReport) -> dict:
    """Everything the model may use - stored rows only, nulls left as nulls."""
    ticker = session.get(Ticker, report.ticker_id)
    payload = report.payload_json or {}
    today = dt.date.today()
    bars = closes_with_dates(session, ticker.id)
    last_close = {"date": bars[-1][0], "close": bars[-1][1]} if bars else None
    # ~monthly samples over the last year: shape of the trend, few tokens.
    year = bars[-253:]
    price_path = [{"date": d, "close": c} for d, c in year[::21]]
    if year and (not price_path or price_path[-1]["date"] != year[-1][0]):
        price_path.append({"date": year[-1][0], "close": year[-1][1]})

    fund = session.scalar(
        select(Fundamentals).where(Fundamentals.ticker_id == ticker.id)
        .order_by(Fundamentals.as_of_date.desc()).limit(1)
    )
    fundamentals = None
    if fund is not None:
        fundamentals = {"as_of": fund.as_of_date}
        for col in Fundamentals.__table__.columns:
            if col.name in ("id", "ticker_id", "as_of_date", "source", "fetched_at"):
                continue
            key = f"{col.name}_pct" if col.type is PCT and not col.name.endswith("_pct") \
                else col.name
            fundamentals[key] = getattr(fund, col.name)

    news = session.scalars(
        select(NewsItem).where(
            NewsItem.ticker_id == ticker.id,
            NewsItem.published_at >= dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30),
        ).order_by(NewsItem.published_at.desc()).limit(15)
    ).all()
    actions = session.scalars(
        select(AnalystAction).where(
            AnalystAction.ticker_id == ticker.id,
            AnalystAction.action_date >= today - dt.timedelta(days=90),
        ).order_by(AnalystAction.action_date.desc()).limit(15)
    ).all()
    reported = session.scalars(
        select(EarningsEvent).where(EarningsEvent.ticker_id == ticker.id,
                                    EarningsEvent.status == "reported")
        .order_by(EarningsEvent.event_date.desc()).limit(4)
    ).all()
    upcoming = session.scalar(
        select(EarningsEvent).where(EarningsEvent.ticker_id == ticker.id,
                                    EarningsEvent.event_date >= today,
                                    EarningsEvent.status != "reported")
        .order_by(EarningsEvent.event_date).limit(1)
    )

    return {
        "today": today,
        "ticker": {"symbol": ticker.symbol, "name": ticker.name,
                   "sector": ticker.sector, "industry": ticker.industry},
        "last_close": last_close,
        "price_path_1y_monthly": price_path,
        "indicators": payload.get("indicators"),
        "fundamentals": fundamentals,
        "rules_baseline": {
            "composite_score": report.score,
            "stance": report.overall_stance,
            "sub_scores": payload.get("sub_scores"),
            "risk_flags": payload.get("risks"),
            "unavailable_inputs": payload.get("missing_fields"),
        },
        "news_30d": [
            {"published": n.published_at, "source": n.source, "title": n.title,
             "excerpt": (n.body_excerpt or "")[:300] or None,
             "sentiment_pct": n.sentiment_score}
            for n in news
        ],
        "analyst_actions_90d": [
            {"date": a.action_date, "firm": a.firm, "action": a.action,
             "from_rating": a.old_rating, "to_rating": a.new_rating,
             "old_target": a.old_target, "new_target": a.new_target}
            for a in actions
        ],
        "earnings_reported": [
            {"date": e.event_date, "eps_estimate": e.eps_estimate, "eps_actual": e.eps_actual,
             "revenue_estimate": e.revenue_estimate, "revenue_actual": e.revenue_actual,
             "surprise_pct": e.surprise_pct}
            for e in reported
        ],
        "next_earnings": {"date": upcoming.event_date, "eps_estimate": upcoming.eps_estimate}
        if upcoming else None,
        # Present only for stocks the emerging lens scored strongly enough
        # to shortlist (see app/research/emerging.py).
        "emerging_signals": _emerging_signals(session, ticker.id),
    }


def _emerging_signals(session: Session, ticker_id: int) -> dict | None:
    row = latest_emerging(session, ticker_id)
    if row is None or row.emerging_score < emerging.SHORTLIST_MIN_SCORE:
        return None
    return {"lens_score": row.emerging_score, "lens_rank": row.emerging_rank,
            **(row.emerging or {})}


# --------------------------------------------------------------- validation

def validate_analysis(raw: dict) -> dict:
    """Coerce the model's JSON into the shape the app stores, or raise.

    Missing/invalid stance or score is fatal (the report would be
    meaningless); a malformed recommendation just degrades to "none".
    """
    stance = str(raw.get("stance", "")).strip().lower()
    if stance not in STANCES:
        raise LLMError(f"model gave no valid stance (got {raw.get('stance')!r})")
    score = _dec(raw.get("score"))
    if score is None or not D("0") <= score <= D("100"):
        raise LLMError(f"model gave no valid 0-100 score (got {raw.get('score')!r})")
    summary = str(raw.get("summary") or "").strip()
    if not summary:
        raise LLMError("model gave no summary")

    notes: list[str] = []
    call = raw.get("recommendation") if isinstance(raw.get("recommendation"), dict) else {}
    action = str(call.get("action", "none")).strip().lower()
    if action not in ACTIONS:
        notes.append(f"unknown action {action!r} treated as none")
        action = "none"
    if (action, stance) in (("long", "bearish"), ("short", "bullish")) \
            or (action != "none" and stance == "neutral"):
        notes.append(f"{action} contradicts stance {stance}; no call")
        action = "none"
    confidence = _dec(call.get("confidence"))
    if confidence is not None:
        confidence = max(D("0"), min(D("100"), confidence))
    horizon = str(call.get("horizon", "medium")).strip().lower()
    if horizon not in HORIZONS:
        horizon = "medium"
    target, stop = _dec(call.get("target_price")), _dec(call.get("stop_price"))

    return {
        "stance": stance,
        "score": score.quantize(D("0.01")),
        "summary": summary[:2000],
        "bull_points": _points(raw.get("bull_points")),
        "bear_points": _points(raw.get("bear_points")),
        "risks": _points(raw.get("risks")),
        "recommendation": {
            "action": action,
            "confidence": str(confidence) if confidence is not None else None,
            "horizon": horizon,
            "target_price": str(target) if target is not None and target > 0 else None,
            "stop_price": str(stop) if stop is not None and stop > 0 else None,
            "rationale": str(call.get("rationale") or "").strip()[:1000] or None,
        },
        "notes": notes,
    }


def _apply(report: ResearchReport, analysis: dict, version: str) -> None:
    """Make the model's view the report's headline; keep the rules view."""
    payload = dict(report.payload_json or {})
    payload["rules"] = {
        "score": str(report.score),
        "stance": report.overall_stance,
        "summary": report.summary,
        "bull_points": payload.get("bull_points", []),
        "bear_points": payload.get("bear_points", []),
        "risks": payload.get("risks", []),
    }
    payload["bull_points"] = analysis["bull_points"]
    payload["bear_points"] = analysis["bear_points"]
    payload["risks"] = analysis["risks"]
    payload["llm"] = {
        "model": version.removeprefix("llm:"),
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "recommendation": analysis["recommendation"],
        "notes": analysis["notes"],
    }
    report.payload_json = payload
    report.summary = analysis["summary"]
    report.overall_stance = analysis["stance"]
    report.score = analysis["score"]
    report.model_or_rule_version = version


def _levels(action: str, entry: Decimal, stop: Decimal | None,
            target: Decimal | None) -> tuple[Decimal, Decimal, str]:
    """Model levels if they're on the right side of entry and sane, else defaults."""
    if action == "long":
        stop_ok = stop is not None and entry * D("0.5") <= stop < entry
        target_ok = target is not None and entry < target <= entry * D("3")
        default_stop, default_target = entry * D("0.85"), entry * D("1.25")
    else:
        stop_ok = stop is not None and entry < stop <= entry * D("1.5")
        target_ok = target is not None and entry * D("0.3") <= target < entry
        default_stop, default_target = entry * D("1.15"), entry * D("0.85")
    fixed: list[str] = []
    if not stop_ok:
        stop = default_stop
        fixed.append("stop")
    if not target_ok:
        target = default_target
        fixed.append("target")
    note = f"default {' + '.join(fixed)} used: model level missing or implausible" \
        if fixed else ""
    return stop, target, note


def _points(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    out = [str(p).strip()[:MAX_POINT_CHARS] for p in raw if str(p).strip()]
    return out[:MAX_POINTS]


def _dec(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = D(str(value).strip().rstrip("%").replace(",", ""))
    except InvalidOperation:
        return None
    return result if result.is_finite() else None
