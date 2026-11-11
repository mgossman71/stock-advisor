"""Research engine: indicators, rule-based scoring, evidence-backed reports."""

from app.research.engine import AdvisorEngine, RuleBasedAdvisor
from app.research.report import build_report

__all__ = ["AdvisorEngine", "RuleBasedAdvisor", "build_report"]
