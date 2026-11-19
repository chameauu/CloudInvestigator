"""Restart drill — the Phase 1 exit gate (interrupt → crash → resume → ledger match).

Usage::

    DATABASE_URL=postgresql://user:pass@localhost:5433/db \\
        uv run python scripts/restart_drill.py

Runs one investigation fully (checkpointer + ledger), then re-runs the same
scenario interrupted at the correlate boundary, "crashes", resumes from the
checkpoint with the same thread_id, and asserts the resumed report equals the
fresh one and the ledger rows match. Prints PASS/FAIL + counts; the exit code
follows.
"""

from cloud_investigator.config import load_settings
from cloud_investigator.engine.graph import DEFAULT_SCENARIO
from cloud_investigator.store.db import redact_dsn
from cloud_investigator.store.drill import run_restart_drill


def _counts_line(label: str, counts: dict) -> str:
    return (
        f"  {label}: evidence={counts['evidence']} tool_calls={counts['tool_calls']} "
        f"hypotheses={counts['hypotheses']} statuses={counts['hypothesis_statuses']} "
        f"reports={counts['reports']} sections={counts['report_sections']}"
    )


def main() -> int:
    settings = load_settings()
    print(f"restart drill: ledger {redact_dsn(settings.database_url)}")
    result = run_restart_drill(DEFAULT_SCENARIO, settings=settings)

    print(_counts_line("run A (fresh, full)      ", result.counts_a))
    print(_counts_line("run B (interrupt+resumed) ", result.counts_b))

    if result.ok:
        print("PASS — resumed report == fresh report; ledger rows match between both runs")
        return 0
    for reason in result.reasons:
        print(f"  FAIL: {reason}")
    print("FAIL — restart drill did not pass")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
