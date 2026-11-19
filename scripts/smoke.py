"""Step 0 smoke — run the pinned graph once, fully offline, assert the pass gate.

Exit 0 iff:
  - one typed InvestigationReport came out end-to-end
  - >= 2 hypotheses, at least one confirmed
  - >= 1 finding, and EVERY finding evidence_id resolves to state evidence
  - >= 2 ToolCallRecords in coverage
  - the poisoned hypothesis was demoted to unverified (verify guard works)
  - findings are grounded in runbook citations
"""

from cloud_investigator.contracts import InvestigationReport
from cloud_investigator.engine.graph import DEFAULT_SCENARIO, run_investigation


def main() -> int:
    report = run_investigation(scenario=DEFAULT_SCENARIO)

    assert isinstance(report, InvestigationReport), "not a typed InvestigationReport"
    assert report.investigation_id, "report has no investigation_id"

    valid_ids = {e.id for e in report.evidence}
    confirmed = [h for h in report.hypotheses if h.status == "confirmed"]
    unverified = [h for h in report.hypotheses if h.status == "unverified"]

    assert len(report.hypotheses) >= 2, "need >= 2 competing hypotheses"
    assert confirmed, "need >= 1 confirmed hypothesis"
    assert report.findings, "need >= 1 finding"
    for f in report.findings:
        bad = [i for i in f.evidence_ids if i not in valid_ids]
        assert not bad, f"finding cites unknown evidence ids: {bad}"
    assert report.coverage.tool_calls >= 2, "need >= 2 tool call records"
    assert report.has_findings and not report.abstained, "expected a non-abstaining report"

    # Spike-specific guards (prove the machinery, not just the happy path)
    assert unverified, "poisoned hypothesis was not demoted to unverified by verify"
    assert all(f.citations for f in report.findings), "findings must carry runbook citations"
    assert all(i in valid_ids for t in report.timeline for i in t.evidence_ids), (
        "timeline cites unknown evidence"
    )

    print("SMOKE OK — typed InvestigationReport end-to-end, fully offline")
    print(
        f"  hypotheses : {len(report.hypotheses)} "
        f"(confirmed={len(confirmed)}, unverified={len(unverified)})"
    )
    print(
        f"  findings   : {len(report.findings)}  evidence: {len(report.evidence)}  "
        f"tool_calls: {report.coverage.tool_calls}"
    )
    print()
    print(report.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
