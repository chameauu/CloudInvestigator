"""Restart drill — the Phase 1 exit-gate proof (AGENTS §10).

With a real checkpointer + ledger:
1. Run investigation A fully on its production thread
   (``thread_id = investigation_id``), persist it.
2. Wipe the investigation's ledger rows (proving ON DELETE CASCADE), then
   run investigation B of the same scenario, interrupted at the correlate
   boundary, and "crash". B runs on its own thread id
   (``{investigation_id}:restart``): invoking with input on A's thread would
   UPDATE A's checkpointed state (merge_list reducers would append), while B
   is a separate execution of the same scenario — its interrupt and resume
   legs share one thread, which is the pair that must match.
3. Prove the checkpoint captured the probe results: a NEW graph instance
   reads the thread state back from Postgres.
4. Resume from the checkpoint with the same thread_id, persist, and assert:
   - the resumed report equals the fresh report (determinism), excluding
     ``created_at`` — a wall-clock stamp, not part of determinism;
   - the ledger rows (evidence, tool_calls, hypotheses statuses) match run A.

The wipe between runs is what makes the ledger assertion meaningful: the
resumed run must genuinely re-write 4 evidence rows into an empty ledger, so a
resume that lost probe results cannot pass.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..config import Settings, load_settings
from ..engine.graph import (
    DEFAULT_SCENARIO,
    build_graph,
    initial_state,
    run_investigation,
)
from .checkpointer import build_postgres_checkpointer, close_checkpointer
from .db import apply_migrations, connect, default_migrations_dir, redact_dsn
from .ledger import delete_investigation, ledger_counts

#: The drill interrupts here: everything before is the parallel probe fan-out,
#: everything after is single-branch analysis — the natural crash boundary.
INTERRUPT_NODE = "correlate"


@dataclass
class DrillResult:
    """Restart drill outcome — scripts print this; tests assert on it."""

    ok: bool
    reasons: list[str] = field(default_factory=list)
    counts_a: dict = field(default_factory=dict)
    counts_b: dict = field(default_factory=dict)


def _report_diff(report_a, report_b) -> list[str]:
    """Names of report fields that differ, excluding the created_at stamp."""
    dump_a = report_a.model_dump(mode="json")
    dump_b = report_b.model_dump(mode="json")
    dump_a.pop("created_at", None)
    dump_b.pop("created_at", None)
    return sorted(k for k in dump_a if dump_a[k] != dump_b.get(k))


def run_restart_drill(
    scenario: str = DEFAULT_SCENARIO,
    settings: Settings | None = None,
    migrations_dir: Path | None = None,
) -> DrillResult:
    """Run the interrupt → crash → resume → compare drill. See module docstring."""
    settings = settings or load_settings()
    migrations_dir = migrations_dir or default_migrations_dir()
    reasons: list[str] = []
    # Ids are run-scoped (minted per execution), so the drill pins its own:
    # both legs must write to the same ledger rows to be comparable.
    investigation_id = f"inv:drill:{scenario}"
    # B is a separate execution of the same scenario: its own thread, shared by
    # the interrupt leg and the resume leg (see module docstring).
    restart_thread = f"{investigation_id}:restart"
    config = {"configurable": {"thread_id": restart_thread}}

    # Schema first (idempotent) — the drill is also a migrate smoke test.
    with connect(settings) as conn:
        apply_migrations(conn, migrations_dir)

    checkpointer = build_postgres_checkpointer(settings.database_url)
    try:
        # A fresh execution needs a fresh thread: `invoke(input)` on a used
        # thread UPDATES the latest checkpointed state (merge_list would
        # append) instead of starting clean. Clearing both threads first also
        # makes the drill re-runnable against a warm database.
        checkpointer.delete_thread(investigation_id)
        checkpointer.delete_thread(restart_thread)

        # ---- Run A: fresh, full, persisted.
        delete_investigation(investigation_id)  # clean slate, ignore absence
        report_a = run_investigation(
            scenario=scenario,
            persist=True,
            checkpointer=checkpointer,
            # Pin the thread too: the default scenario-level thread would be
            # shared by every drill of this scenario, and merge_list reducers
            # would append a second run's probes to the first's checkpoint.
            thread_id=investigation_id,
            investigation_id=investigation_id,
        )
        counts_a = ledger_counts(investigation_id)
        if counts_a["evidence"] != len(report_a.evidence):
            reasons.append(
                f"run A ledger evidence {counts_a['evidence']} != report {len(report_a.evidence)}"
            )
        if counts_a["tool_calls"] != report_a.coverage.tool_calls:
            reasons.append(
                f"run A ledger tool_calls {counts_a['tool_calls']} != coverage "
                f"{report_a.coverage.tool_calls}"
            )
        if counts_a["hypotheses"] != len(report_a.hypotheses):
            reasons.append(
                f"run A ledger hypotheses {counts_a['hypotheses']} != report "
                f"{len(report_a.hypotheses)}"
            )

        # ---- Wipe: prove cascade and reset the comparison baseline.
        delete_investigation(investigation_id)
        wiped = ledger_counts(investigation_id)
        if wiped["present"] or wiped["evidence"] or wiped["tool_calls"] or wiped["hypotheses"]:
            reasons.append(f"cascade wipe failed — rows survived: {wiped}")

        # ---- Run B: same thread, interrupted at the correlate boundary.
        crashed_app = build_graph(checkpointer=checkpointer, interrupt_before=[INTERRUPT_NODE])
        partial = crashed_app.invoke(
            initial_state("", scenario, investigation_id=investigation_id), config=config
        )
        del crashed_app  # "crash": the process is gone; only Postgres remains.
        if partial.get("report") is not None:
            reasons.append("interrupted leg completed a report — interrupt did not fire")

        # ---- Restart proof: a NEW graph instance reads the probe results
        # back from the checkpoint (they must already be there, pre-correlate).
        fresh_app = build_graph(checkpointer=checkpointer)
        snap = fresh_app.get_state(config)
        checkpointed_evidence = len(snap.values.get("evidence") or [])
        if checkpointed_evidence != len(report_a.evidence):
            reasons.append(
                f"checkpoint holds {checkpointed_evidence} evidence items, "
                f"expected {len(report_a.evidence)} — probe results not recovered"
            )

        # ---- Resume from the checkpoint, same thread, and persist.
        report_b = run_investigation(
            scenario=scenario,
            persist=True,
            checkpointer=checkpointer,
            thread_id=restart_thread,
            resume=True,
        )
        counts_b = ledger_counts(investigation_id)

        # ---- Determinism: resumed report == fresh report (minus created_at).
        differing = _report_diff(report_a, report_b)
        if differing:
            reasons.append(f"resumed report differs from fresh report: {differing}")

        # ---- Ledger match between both runs.
        for key in ("evidence", "tool_calls", "hypotheses", "hypothesis_statuses"):
            if counts_a[key] != counts_b[key]:
                reasons.append(
                    f"ledger {key} mismatch: run A {counts_a[key]!r} vs run B {counts_b[key]!r}"
                )
        if counts_b["evidence"] != len(report_b.evidence):
            reasons.append(
                f"run B ledger evidence {counts_b['evidence']} != resumed report "
                f"{len(report_b.evidence)}"
            )
    finally:
        close_checkpointer(checkpointer)

    return DrillResult(ok=not reasons, reasons=reasons, counts_a=counts_a, counts_b=counts_b)


__all__ = ["INTERRUPT_NODE", "DrillResult", "run_restart_drill", "redact_dsn"]
