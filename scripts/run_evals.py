"""Replay every golden scenario twice and score the reports — fully offline.

Scenarios marked ``held_out`` were authored after the scoring rules were frozen
and are never tuned against; they exist to detect overfitting. They are replayed,
scored, and shown in a separate table, but excluded from the headline metrics.

Exit code is 0 only when, for all scenarios (headline and held-out):
  * the two replays produce identical reports (determinism),
  * the top finding names the expected component and fault class,
  * abstention matches the expectation,
  * no forbidden term leaked into a claim the report asks a reader to act on.
"""

from __future__ import annotations

import sys
from pathlib import Path

from cloud_investigator.engine.graph import run_investigation
from cloud_investigator.eval import (
    GoldenScenario,
    ScenarioScore,
    build_verdict,
    load_golden,
    same_report,
    score_report,
    split_scenarios,
    summarize,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
GOLDEN = REPO_ROOT / "evals" / "golden.yaml"

HEADER = (
    f"{'scenario':<26} {'comp':<5} {'fault':<5} {'valid%':>7} {'abstain':<8} {'replay':<7} notes"
)


def _yes(flag: bool) -> str:
    return "ok" if flag else "FAIL"


def _replay(scenarios: list[GoldenScenario]) -> tuple[list[tuple[ScenarioScore, bool]], bool]:
    """Replay each scenario twice and score the first report."""
    rows: list[tuple[ScenarioScore, bool]] = []
    deterministic = True
    for scenario in scenarios:
        first = run_investigation(scenario.question, scenario.name)
        second = run_investigation(scenario.question, scenario.name)
        replay_same = same_report(first, second)
        deterministic = deterministic and replay_same
        rows.append((score_report(scenario.name, first, scenario.expectation), replay_same))
    return rows, deterministic


def _print_table(title: str, rows: list[tuple[ScenarioScore, bool]]) -> None:
    print(title)
    print(HEADER)
    print("-" * len(HEADER))
    for score, replay_same in rows:
        notes = []
        if score.forbidden_hits:
            notes.append("forbidden: " + ", ".join(score.forbidden_hits))
        if score.claims_total:
            notes.append(f"{score.claims_valid}/{score.claims_total} claims cited")
        print(
            f"{score.scenario:<26} {_yes(score.component_ok):<5} {_yes(score.fault_class_ok):<5} "
            f"{score.evidence_pct:>6.1f}% {_yes(score.abstention_ok):<8} {_yes(replay_same):<7} "
            + "; ".join(notes)
        )
    print()


def main() -> int:
    scenarios = load_golden(GOLDEN)
    split = split_scenarios(scenarios)

    print(
        f"eval: {len(scenarios)} golden scenario(s) from {GOLDEN.name} "
        f"({len(split.headline)} headline, {len(split.held_out)} held-out)"
    )
    print()

    headline_rows, headline_deterministic = _replay(list(split.headline))
    held_out_rows, held_out_deterministic = _replay(list(split.held_out))

    _print_table("headline scenarios (scored)", headline_rows)
    _print_table(
        "held-out scenarios (overfitting check, excluded from headline metrics)", held_out_rows
    )

    headline_scores = [score for score, _ in headline_rows]
    held_out_scores = [score for score, _ in held_out_rows]

    summary = summarize(headline_scores, determinism_ok=headline_deterministic)
    print(f"component accuracy   : {summary.component_accuracy:.1%}")
    print(f"fault-class accuracy : {summary.fault_class_accuracy:.1%}")
    print(f"evidence validity    : {summary.evidence_validity_pct:.1f}%")
    print(f"abstention rate      : {summary.abstention_rate:.1%}")
    print(f"replay determinism   : {'identical' if summary.determinism_ok else 'MISMATCH'}")

    verdict = build_verdict(
        headline_scores,
        held_out_scores,
        headline_deterministic=headline_deterministic,
        held_out_deterministic=held_out_deterministic,
    )
    print()
    print(verdict.message)
    return 0 if verdict.ok else 1


if __name__ == "__main__":
    sys.exit(main())
