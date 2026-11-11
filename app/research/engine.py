"""The ``AdvisorEngine`` seam: research now, recommend from the research.

``RuleBasedAdvisor`` ships v1's explainable rules.  An LLM engine can be
swapped in by implementing the same protocol - the ``engine_version`` stamped
on every recommendation is what makes the two comparable later.

Recommendation rules (pinned by ``tests/test_advisor.py``):

* LONG  when stance is bullish and composite >= 65.
* SHORT when stance is bearish and composite <= 35, or price sits >= 10 %
  above the consensus target *and* there were >= 2 more downgrades than
  upgrades in the last 90 days.
* Never emits a call with a fabricated entry price: no stored price -> no call.
* One open call per (ticker, direction) at a time - nightly re-runs are
  idempotent and won't spam the scoreboard.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Recommendation, ResearchReport, Ticker
from app.research.report import build_report
from app.trading.orders import latest_price

D = Decimal


class AdvisorEngine(Protocol):
    """What every advisor implementation must provide."""

    version: str

    def research(self, session: Session, symbol: str) -> ResearchReport | None:
        """Run the full research pipeline and persist the report."""
        ...

    def recommendations_for(self, session: Session,
                            report: ResearchReport) -> list[Recommendation]:
        """Emit (and persist) recommendations justified by ``report``."""
        ...


class RuleBasedAdvisor:
    """v1 engine: thresholds over the five sub-scores, plain and citable."""

    version = "rules-v1"

    LONG_MIN_SCORE = D("65")
    SHORT_MAX_SCORE = D("35")
    OVEREXTENDED_UPSIDE_PCT = D("-10")  # price >= 10 % above consensus target
    DOWNGRADE_NET = -2                   # >= 2 more downgrades than upgrades

    def research(self, session: Session, symbol: str) -> ResearchReport | None:
        return build_report(session, symbol, persist=True)

    def recommendations_for(self, session: Session,
                            report: ResearchReport) -> list[Recommendation]:
        payload = report.payload_json or {}
        bull = payload.get("bull_points", [])
        bear = payload.get("bear_points", [])
        missing = payload.get("missing_fields", [])
        rationale_parts = (bull if report.overall_stance == "bullish" else bear) or []

        ticker = report.ticker
        priced = latest_price(session, ticker.id)
        if priced is None:
            return []  # no stored price -> no call (never invent an entry)
        entry, as_of = priced

        calls: list[Recommendation] = []
        if report.overall_stance == "bullish" and report.score >= self.LONG_MIN_SCORE:
            calls.append(self._make(
                session, report, "long", entry,
                confidence=self._confidence(report.score, D("60"), missing),
                stop=entry * D("0.85"),
                target=self._long_target(entry, payload),
                rationale=" | ".join(rationale_parts[:4]) or report.summary,
            ))
        elif report.overall_stance == "bearish" and report.score <= self.SHORT_MAX_SCORE:
            calls.append(self._make(
                session, report, "short", entry,
                confidence=self._confidence(D("100") - report.score, D("60"), missing),
                stop=entry * D("1.15"),
                target=entry * D("0.85"),
                rationale=" | ".join(rationale_parts[:4]) or report.summary,
            ))
        elif self._overextended(payload) is not None:
            upside = self._overextended(payload)
            calls.append(self._make(
                session, report, "short", entry,
                confidence=self._confidence(D("100") - report.score, D("50"), missing),
                stop=entry * D("1.10"),
                target=entry * D("0.85"),
                rationale=(
                    f"price is {upside}% against the consensus target with a cluster "
                    f"of downgrades; composite {report.score}/100"
                ),
            ))

        return [c for c in calls if c is not None]

    # ---------------------------------------------------------------- rules

    def _overextended(self, payload: dict) -> Decimal | None:
        """Price far above consensus target AND >= 2 net downgrades -> short setup.

        Returns the (negative) upside-to-target when the setup fires, else None.
        Reads the numeric counts from ``payload["indicators"]``; reports built
        before those keys existed fall back to the cited evidence row
        (``"+3/-5"``).
        """
        ind = payload.get("indicators", {})
        up, down = ind.get("analyst_upgrades_90d"), ind.get("analyst_downgrades_90d")
        upside = _dec(ind.get("target_upside_pct"))
        evidence = {e.get("label"): e.get("value") for e in payload.get("evidence", [])}
        if up is None or down is None:
            raw = evidence.get("analyst actions 90d")
            try:
                plus, minus = str(raw).split("/-")
                up, down = int(plus.lstrip("+")), int(minus)
            except (AttributeError, ValueError):
                return None
        if upside is None:
            upside = _dec(evidence.get("upside to consensus target %"))
        if upside is None:
            return None
        if down - up >= -self.DOWNGRADE_NET and upside <= self.OVEREXTENDED_UPSIDE_PCT:
            return upside.quantize(D("0.01"))
        return None

    def _confidence(self, magnitude: Decimal, base: Decimal, missing: list) -> Decimal:
        """Map score distance above ``base`` onto 50..95, minus 2 per missing field."""
        raw = D("50") + (magnitude - base) * D("1.25")
        raw -= D("2") * len(missing)
        return max(D("10"), min(D("95"), raw)).quantize(D("0.1"))

    def _long_target(self, entry: Decimal, payload: dict) -> Decimal:
        evidence = payload.get("evidence", [])
        target_row = next((e for e in evidence if e.get("label", "").startswith("upside")), None)
        floor = entry * D("1.15")
        ceiling = entry * D("1.50")
        if target_row and target_row.get("value") not in (None, "None"):
            try:
                upside = D(target_row["value"])
                implied = entry * (D("1") + upside / D("100"))
                return max(floor, min(ceiling, implied))
            except Exception:
                pass
        return entry * D("1.25")

    def _make(self, session: Session, report: ResearchReport, direction: str,
              entry: Decimal, *, confidence: Decimal, stop: Decimal, target: Decimal,
              rationale: str, horizon: str = "medium",
              engine_version: str | None = None) -> Recommendation | None:
        """Persist one recommendation unless an open call already exists."""
        open_existing = session.scalar(
            select(Recommendation).where(
                Recommendation.ticker_id == report.ticker_id,
                Recommendation.direction == direction,
                Recommendation.status == "open",
            ).limit(1)
        )
        if open_existing is not None:
            return None
        rec = Recommendation(
            ticker_id=report.ticker_id,
            report_id=report.id,
            direction=direction,
            horizon=horizon,
            generated_at=dt.datetime.now(dt.timezone.utc),
            entry_price_at_call=entry,
            stop_price=stop,
            target_price=target,
            status="open",
            confidence=confidence,
            rationale=rationale,
            evidence_json=(report.payload_json or {}).get("evidence", []),
            engine_version=engine_version or self.version,
        )
        session.add(rec)
        session.flush()
        return rec


def _dec(value: object) -> Decimal | None:
    if value is None or value == "None":
        return None
    return D(str(value))
