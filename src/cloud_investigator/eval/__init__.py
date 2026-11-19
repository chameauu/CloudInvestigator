"""Offline evaluation: golden expectations, replay determinism, and scoring."""

from __future__ import annotations

from .golden import Expectation, GoldenScenario, load_golden
from .replay import report_fingerprint, same_report
from .scorer import EvalSummary, ScenarioScore, score_report, summarize

__all__ = [
    "EvalSummary",
    "Expectation",
    "GoldenScenario",
    "ScenarioScore",
    "load_golden",
    "report_fingerprint",
    "same_report",
    "score_report",
    "summarize",
]
