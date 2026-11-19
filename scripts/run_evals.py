"""Replay every golden scenario twice and score the reports — fully offline.

Exit code is 0 only when, for all scenarios:
  * the two replays produce identical reports (determinism),
  * the top finding names the expected component and fault class,
  * abstention matches the expectation,
  * no forbidden term leaked into a claim the report asks a reader to act on.
"""

from __future__ import annotations

import sys
from pathlib import Path

from cloud_investigator.engine.graph import run_investigation
from cloud_investigator.eval import load_golden, same_report, score_report, summarize

REPO_ROOT = Path(__file__).resolve().parents[1]
GOLDEN = REPO_ROOT / "evals" / "golden.yaml"


def _yes(flag: bool) -> str:
    return "ok" if flag else "FAIL"


def main() -> int:
    scenarios = load_golden(GOLDEN)
    scores = []
    deterministic = True

    print(f"eval: {len(scenarios)} golden scenario(s) from {GOLDEN.name}")
    print()
    header = (
        f"{'scenario':<26} {'comp':<5} {'fault':<5} {'valid%':>7} "
        f"{'abstain':<8} {'replay':<7} notes"
    )
    print(header)
    print("-" * len(header))

    for scenario in scenarios:
        first = run_investigation(scenario.question, scenario.name)
        second = run_investigation(scenario.question, scenario.name)
        replay_same = same_report(first, second)
        deterministic = deterministic and replay_same

        score = score_report(scenario.name, first, scenario.expectation)
        scores.append(score)

        notes = []
        if score.forbidden_hits:
            notes.append("forbidden: " + ", ".join(score.forbidden_hits))
        if score.claims_total:
            notes.append(f"{score.claims_valid}/{score.claims_total} claims cited")
        print(
            f"{scenario.name:<26} {_yes(score.component_ok):<5} {_yes(score.fault_class_ok):<5} "
            f"{score.evidence_pct:>6.1f}% {_yes(score.abstention_ok):<8} {_yes(replay_same):<7} "
            + "; ".join(notes)
        )

    summary = summarize(scores, determinism_ok=deterministic)
    print()
    print(f"component accuracy   : {summary.component_accuracy:.1%}")
    print(f"fault-class accuracy : {summary.fault_class_accuracy:.1%}")
    print(f"evidence validity    : {summary.evidence_validity_pct:.1f}%")
    print(f"abstention rate      : {summary.abstention_rate:.1%}")
    print(f"replay determinism   : {'identical' if summary.determinism_ok else 'MISMATCH'}")

    failures = [
        score.scenario
        for score in scores
        if not (score.component_ok and score.fault_class_ok and score.abstention_ok)
        or score.forbidden_hits
    ]
    if not deterministic:
        failures.append("<replay determinism>")
    if failures:
        print()
        print("EVALS FAILED — " + ", ".join(failures))
        return 1

    print()
    print(
        "EVALS OK — conformance replay: expected component/fault per scenario,"
        " deterministic replays, no forbidden claims"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
