"""Golden-eval split and verdict logic.

Held-out scenarios are replayed and scored, then excluded from the headline
metrics. They exist to detect overfitting: they were authored after the scoring
rules were frozen and are never tuned against. A held-out failure still fails
the run.
"""

from __future__ import annotations

from dataclasses import dataclass

from .golden import GoldenScenario
from .scorer import ScenarioScore


@dataclass(frozen=True)
class ScenarioSplit:
    """Golden scenarios partitioned into headline and held-out groups."""

    headline: tuple[GoldenScenario, ...]
    held_out: tuple[GoldenScenario, ...]


def split_scenarios(scenarios: list[GoldenScenario]) -> ScenarioSplit:
    """Partition scenarios, preserving their file order within each group."""
    return ScenarioSplit(
        headline=tuple(scenario for scenario in scenarios if not scenario.held_out),
        held_out=tuple(scenario for scenario in scenarios if scenario.held_out),
    )


@dataclass(frozen=True)
class EvalVerdict:
    """Outcome of the harness, keeping headline and held-out failures distinct."""

    ok: bool
    headline_failures: tuple[str, ...] = ()
    held_out_failures: tuple[str, ...] = ()
    message: str = ""


def scenario_passes(score: ScenarioScore) -> bool:
    """Whether one scored scenario met every expectation."""
    return (
        score.component_ok
        and score.fault_class_ok
        and score.abstention_ok
        and not score.forbidden_hits
    )


def _failures(scores: list[ScenarioScore], deterministic: bool) -> tuple[str, ...]:
    failures = [score.scenario for score in scores if not scenario_passes(score)]
    if not deterministic:
        failures.append("<replay determinism>")
    return tuple(failures)


def build_verdict(
    headline_scores: list[ScenarioScore],
    held_out_scores: list[ScenarioScore],
    *,
    headline_deterministic: bool,
    held_out_deterministic: bool,
) -> EvalVerdict:
    """Build the exit verdict; held-out scenarios can fail but never hide in the headline."""
    headline_failures = _failures(headline_scores, headline_deterministic)
    held_out_failures = _failures(held_out_scores, held_out_deterministic)
    if not headline_failures and not held_out_failures:
        held_out_total = len(held_out_scores)
        return EvalVerdict(
            ok=True,
            message=(
                "EVALS OK — conformance replay: expected component/fault per scenario,"
                " deterministic replays, no forbidden claims;"
                f" held-out: {held_out_total}/{held_out_total}"
            ),
        )

    parts: list[str] = []
    if headline_failures:
        parts.append("headline: " + ", ".join(headline_failures))
    if held_out_failures:
        parts.append("held-out: " + ", ".join(held_out_failures))
    return EvalVerdict(
        ok=False,
        headline_failures=headline_failures,
        held_out_failures=held_out_failures,
        message="EVALS FAILED — " + "; ".join(parts),
    )
