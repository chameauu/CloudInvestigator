"""Click-through demo of one investigation — fully offline.

    uv run python scripts/demo.py                     # s3_access_denied
    uv run python scripts/demo.py --scenario ecs_task_oom
    uv run python scripts/demo.py --list

Recorded cassettes only: no cloud credentials, no API keys, no database.
The same report is produced by ``make run`` (smoke) and scored by ``make eval``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from cloud_investigator.contracts import InvestigationReport, TimeRange
from cloud_investigator.engine.graph import get_graph, initial_state
from cloud_investigator.eval import load_golden
from cloud_investigator.sources.fake import load_cassette

REPO_ROOT = Path(__file__).resolve().parents[1]
GOLDEN = REPO_ROOT / "evals" / "golden.yaml"

BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
CYAN = "\033[36m"


def _window(time_range: TimeRange | None) -> str:
    if time_range is None:
        return "default window"
    if time_range.label:
        return time_range.label
    return f"{time_range.start:%Y-%m-%d %H:%M}Z → {time_range.end:%Y-%m-%d %H:%M}Z"


def _step(node: str, text: str = "") -> None:
    print(f"\n{BOLD}{CYAN}[{node}]{RESET} {text}")


def _mark(status: str) -> str:
    return {
        "confirmed": f"{GREEN}✓{RESET}",
        "refuted": f"{RED}✗{RESET}",
        "unverified": f"{YELLOW}?{RESET}",
        "ok": f"{GREEN}ok{RESET}",
        "no_data": f"{YELLOW}no_data{RESET}",
        "error": f"{RED}error{RESET}",
        "denied": f"{RED}denied{RESET}",
        "skipped_budget": f"{YELLOW}skipped{RESET}",
    }.get(status, status)


def _render_gate(update: dict) -> None:
    integration = update.get("integration") or {}
    available = integration.get("available") or []
    unavailable = integration.get("unavailable") or []
    display = integration.get("display_name") or integration.get("service") or "?"
    _step("gate", f"catalog service {display} — {len(available)} source(s) usable")
    for source in available:
        scope = []
        params = source.get("params") or {}
        if params.get("log_groups"):
            scope.append("log group " + ", ".join(params["log_groups"]))
        if params.get("filter_pattern"):
            scope.append(f"filter {params['filter_pattern']!r}")
        if params.get("databases"):
            scope.append("database " + ", ".join(params["databases"]))
        detail = f" — {'; '.join(scope)}" if scope else ""
        print(f"  {_mark('ok')} {source.get('name')}{detail}")
    for source in unavailable:
        print(f"  {_mark('denied')} {source.get('name')} — {source.get('reason')}")
    if update.get("abstain_reason"):
        print(f"  {RED}abstain:{RESET} {update['abstain_reason']}")


def _field(obj, name: str, default=None):
    """Read a field from either a model instance or its dict form."""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _render_probe(update: dict) -> None:
    calls = update.get("tool_calls") or []
    evidence = update.get("evidence") or []
    for call in calls:
        status = _field(call, "status", "")
        source = _field(call, "source", "?")
        operation = str(_field(call, "operation", ""))[:70]
        duration = _field(call, "duration_ms", 0)
        print(f"  {_mark(status)} {source}: {operation} {DIM}({duration} ms){RESET}")
        error = _field(call, "error")
        if error:
            print(f"      {DIM}{RED}{str(error)[:120]}{RESET}")
    if evidence:
        ids = ", ".join(str(_field(item, "id", "?")) for item in evidence)
        print(f"  {DIM}→ {len(evidence)} evidence item(s): {ids}{RESET}")


def _render_hypothesize(update: dict) -> None:
    candidates = update.get("candidates") or []
    meta = update.get("diagnose_meta") or {}
    mode = "deterministic fallback" if meta.get("used_fallback") else "cassette analyser"
    _step("hypothesize", f"{len(candidates)} competing candidate(s) — {mode}")
    for rank, candidate in enumerate(candidates, start=1):
        print(f"  {rank}. {candidate.statement[:100]}")
    for rejected in meta.get("rejected_ids") or []:
        print(f"  {YELLOW}stripped citation{RESET} {rejected} (no such evidence)")


def _render_eliminate(update: dict) -> None:
    events = update.get("eliminations") or []
    _step("eliminate", f"evidence arrival decides — {len(events)} status change(s)")
    for event in events:
        print(f"  {_mark(event.to_status)} rank {event.candidate_rank}: {event.reason[:110]}")


def _render_verify(update: dict) -> None:
    findings = update.get("findings") or []
    hypotheses = update.get("hypotheses") or []
    failures = (update.get("coverage") or {}).get("failures") or []
    _step(
        "verify",
        f"deterministic citation check — {len(findings)} finding(s), "
        f"{len(hypotheses)} hypothesis(es) kept",
    )
    for failure in failures:
        print(f"  {YELLOW}{failure[:130]}{RESET}")


def _render_report(report: InvestigationReport) -> None:
    print(f"\n{BOLD}{'=' * 78}{RESET}")
    print(
        f"{BOLD}REPORT{RESET} {report.investigation_id}  {DIM}{_window(report.time_range)}{RESET}"
    )
    print(f"\n{report.summary}\n")

    print(f"{BOLD}Hypotheses{RESET}")
    for hypothesis in report.hypotheses:
        print(
            f"  {_mark(hypothesis.status)} #{hypothesis.rank} {hypothesis.status:<10} "
            f"{hypothesis.statement[:90]}"
        )
        for argument in hypothesis.arguments_against[:2]:
            print(f"      {DIM}against: {argument[:100]}{RESET}")

    print(f"\n{BOLD}Findings{RESET}")
    for finding in report.findings:
        resource = finding.resource.display if finding.resource else "—"
        print(f"  [{finding.severity}] {finding.title}  {DIM}({finding.cause_token}){RESET}")
        print(f"      resource: {resource}")
        print(f"      evidence: {', '.join(finding.evidence_ids)}")
        for citation in finding.citations:
            print(f"      runbook : {citation.source} › {citation.section}")

    print(f"\n{BOLD}Recommendations{RESET}")
    for index, recommendation in enumerate(report.recommendations, start=1):
        print(f"  {index}. {recommendation}")

    coverage = report.coverage
    print(f"\n{BOLD}Coverage{RESET}")
    print(f"  planned  : {', '.join(coverage.sources_planned) or '—'}")
    print(f"  queried  : {', '.join(coverage.sources_queried) or '—'}")
    print(f"  skipped  : {', '.join(coverage.skipped) or '—'}")
    print(f"  calls    : {coverage.tool_calls}   failures: {len(coverage.failures)}")
    for failure in coverage.failures:
        print(f"    {DIM}{failure[:120]}{RESET}")

    state = "ABSTAINED" if report.abstained else "not abstained"
    print(
        f"\n  confidence: {report.confidence}   {state}"
        + (f" — {report.abstain_reason}" if report.abstained else "")
    )
    print(f"{BOLD}{'=' * 78}{RESET}")


def _list_scenarios() -> int:
    scenarios = load_golden(GOLDEN)
    print(f"{BOLD}Golden scenarios{RESET} ({GOLDEN.name})\n")
    for scenario in scenarios:
        expectation = scenario.expectation
        target = expectation.component or "—"
        fault = expectation.fault_class or "—"
        abstain = "abstain" if expectation.abstain else "report"
        print(f"  {scenario.name:<26} {target:<12} {fault:<22} {abstain}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", default="s3_access_denied", help="cassette scenario name")
    parser.add_argument("--list", action="store_true", help="list the golden scenarios")
    args = parser.parse_args()

    if args.list:
        return _list_scenarios()

    cassette = load_cassette(args.scenario)
    script = cassette.get("demo_script") or {}

    print(f"{BOLD}CloudInvestigator{RESET} — read-only investigation demo")
    print(f"{DIM}offline · recorded cassette · no credentials, no API keys{RESET}")
    print(f"\n  scenario : {args.scenario}")
    print(f"  question : {cassette.get('question') or script.get('question', '')}")
    print(f"  sources  : {', '.join((script.get('plan') or {}).get('sources') or [])}")

    report: InvestigationReport | None = None
    for chunk in get_graph().stream(
        initial_state(cassette["question"], args.scenario), stream_mode="updates"
    ):
        for node, update in chunk.items():
            update = update or {}
            if node == "intake":
                _step(
                    "intake",
                    f"investigation {update.get('investigation_id', '?')} · "
                    f"service {update.get('service') or '?'} · "
                    f"{_window(update.get('time_range'))}",
                )
            elif node == "plan":
                plan = update.get("plan")
                sources = ", ".join(plan.sources) if plan else "—"
                rationale = (plan.rationale if plan else "")[:110]
                _step("plan", f"sources {sources}")
                if rationale:
                    print(f"  {DIM}{rationale}{RESET}")
            elif node == "gate":
                _render_gate(update)
            elif node == "probe_source":
                _render_probe(update)
            elif node == "correlate":
                _step("correlate", "evidence merged into one timeline")
            elif node == "hypothesize":
                _render_hypothesize(update)
            elif node == "eliminate":
                _render_eliminate(update)
            elif node == "verify":
                _render_verify(update)
            elif node == "report":
                report = update.get("report")

    if not isinstance(report, InvestigationReport):
        print(f"{RED}the graph finished without a typed report{RESET}")
        return 1

    _render_report(report)
    print(
        f"\n{DIM}Replay the same command twice: the report is identical. "
        f"`make eval` scores this scenario offline.{RESET}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
