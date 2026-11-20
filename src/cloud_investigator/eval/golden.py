"""Golden expectations: what a correct investigation looks like per scenario.

The expectations live in ``evals/golden.yaml`` so they are reviewable as data,
not code. One entry per replay cassette in ``fixtures/cassette_<name>.json``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


class GoldenError(ValueError):
    """The golden file is missing, malformed, or internally inconsistent."""


@dataclass(frozen=True)
class Expectation:
    """What the report must get right."""

    component: str = ""
    fault_class: str = ""
    abstain: bool = False
    forbidden_terms: tuple[str, ...] = ()


@dataclass(frozen=True)
class GoldenScenario:
    """One replay scenario plus its expectation.

    ``held_out`` marks a scenario that exists to detect overfitting: it was
    authored after the scoring rules were frozen and is never tuned against.
    It is replayed and scored, but excluded from the headline metrics.
    """

    name: str
    question: str
    expectation: Expectation = field(default_factory=Expectation)
    held_out: bool = False


def load_golden(path: str | Path) -> list[GoldenScenario]:
    """Parse ``evals/golden.yaml`` into typed scenarios (fail-fast on errors)."""
    candidate = Path(path)
    try:
        data = yaml.safe_load(candidate.read_text(encoding="utf-8"))
    except OSError as exc:
        raise GoldenError(f"cannot read golden file {candidate}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise GoldenError(f"invalid YAML in golden file {candidate}: {exc}") from exc

    if not isinstance(data, dict) or not isinstance(data.get("scenarios"), list):
        raise GoldenError(f"golden file {candidate} must contain a 'scenarios' list")
    if not data["scenarios"]:
        raise GoldenError(f"golden file {candidate} contains no scenarios")

    scenarios: list[GoldenScenario] = []
    seen: set[str] = set()
    for index, item in enumerate(data["scenarios"], start=1):
        if not isinstance(item, dict):
            raise GoldenError(f"scenario #{index} must be a mapping")
        name = str(item.get("name", "")).strip()
        question = str(item.get("question", "")).strip()
        if not name:
            raise GoldenError(f"scenario #{index} is missing a name")
        if name in seen:
            raise GoldenError(f"duplicate scenario name {name!r}")
        seen.add(name)
        terms = item.get("forbidden_terms") or []
        scenarios.append(
            GoldenScenario(
                name=name,
                question=question,
                expectation=Expectation(
                    component=str(item.get("component", "")).strip(),
                    fault_class=str(item.get("fault_class", "")).strip(),
                    abstain=bool(item.get("abstain", False)),
                    forbidden_terms=tuple(str(term) for term in terms),
                ),
                held_out=bool(item.get("held_out", False)),
            )
        )
    return scenarios
