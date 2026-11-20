"""Offline evaluation: golden expectations, replay determinism, and scoring."""

from __future__ import annotations

from .golden import Expectation, GoldenScenario, load_golden
from .harness import EvalVerdict, ScenarioSplit, build_verdict, split_scenarios
from .replay import report_fingerprint, same_report
from .scorer import EvalSummary, ScenarioScore, score_report, summarize

__all__ = [
    "EvalSummary",
    "EvalVerdict",
    "Expectation",
    "GoldenScenario",
    "ScenarioScore",
    "ScenarioSplit",
    "build_verdict",
    "load_golden",
    "report_fingerprint",
    "same_report",
    "score_report",
    "split_scenarios",
    "summarize",
]
