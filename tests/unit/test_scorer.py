"""Scorer metrics on synthetic reports — no graph, no database, deterministic."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from cloud_investigator.contracts import (
    EvidenceItem,
    Finding,
    Hypothesis,
    InvestigationReport,
    ResourceRef,
)
from cloud_investigator.eval.golden import Expectation, GoldenScenario, load_golden
from cloud_investigator.eval.replay import report_fingerprint, same_report
from cloud_investigator.eval.scorer import score_report, summarize

EV = ["ev:demo:cloudwatch_logs:1", "ev:demo:cloudwatch_logs:2"]


def _evidence() -> list[EvidenceItem]:
    return [
        EvidenceItem(
            id=EV[0],
            kind="cloudwatch_logs",
            excerpt="first event",
            ts=None,
            query="filter_log_events",
            raw_ref="raw/1",
            truncated=False,
        ),
        EvidenceItem(
            id=EV[1],
            kind="cloudwatch_logs",
            excerpt="second event",
            ts=None,
            query="filter_log_events",
            raw_ref="raw/2",
            truncated=False,
        ),
    ]


def _report(
    *,
    findings: list[Finding] | None = None,
    hypotheses: list[Hypothesis] | None = None,
    abstained: bool = False,
) -> InvestigationReport:
    return InvestigationReport(
        investigation_id="inv:demo",
        question="q",
        summary="s",
        time_range=None,
        timeline=[],
        hypotheses=hypotheses or [],
        findings=findings or [],
        recommendations=[],
        evidence=_evidence() if not abstained else [],
        citations=[],
        coverage={
            "sources_planned": ["cloudwatch_logs"],
            "sources_queried": [],
            "tool_calls": 1,
            "failures": [],
            "skipped": [],
        },
        abstained=abstained,
        abstain_reason="insufficient evidence" if abstained else "",
        confidence="low",
    )


def _resource(service: str = "alb") -> ResourceRef:
    return ResourceRef(service=service, resource_type="load_balancer", name="public-api")


def _finding(
    *,
    service: str = "alb",
    cause_token: str = "TargetHealthDegraded",
    evidence_ids: list[str] | None = None,
    detail: str = "targets unhealthy",
    severity: str = "high",
    confidence: str = "high",
) -> Finding:
    return Finding(
        severity=severity,  # type: ignore[arg-type]
        title="t",
        detail=detail,
        resource=_resource(service),
        cause_token=cause_token,
        confidence=confidence,  # type: ignore[arg-type]
        evidence_ids=EV if evidence_ids is None else evidence_ids,
        recommendation="r",
        citations=[],
    )


# ------------------------------------------------------------------ scoring


def test_perfect_run_scores_every_metric():
    report = _report(findings=[_finding()])
    score = score_report(
        "demo", report, Expectation(component="alb", fault_class="TargetHealthDegraded")
    )
    assert score.component_ok
    assert score.fault_class_ok
    assert score.abstention_ok
    assert score.claims_total == 1
    assert score.claims_valid == 1
    assert score.evidence_pct == 100.0
    assert score.forbidden_hits == ()
    assert not score.abstained


def test_wrong_component_is_detected():
    report = _report(findings=[_finding(service="ecs")])
    score = score_report("demo", report, Expectation(component="alb", fault_class=""))
    assert not score.component_ok
    assert not score.fault_class_ok


def test_wrong_fault_class_is_detected():
    report = _report(findings=[_finding(cause_token="AccessDenied")])
    score = score_report("demo", report, Expectation(component="alb", fault_class="OOMKilled"))
    assert score.component_ok
    assert not score.fault_class_ok


def test_claim_with_unknown_evidence_id_is_invalid():
    report = _report(findings=[_finding(evidence_ids=[EV[0], "ev:demo:sql:999"])])
    score = score_report("demo", report, Expectation(component="alb"))
    assert score.claims_total == 1
    assert score.claims_valid == 0
    assert score.evidence_pct == 0.0


def test_claim_without_any_evidence_id_is_invalid():
    report = _report(findings=[_finding(evidence_ids=[])])
    score = score_report("demo", report, Expectation(component="alb"))
    assert score.claims_valid == 0


def test_confirmed_hypotheses_count_as_claims_but_refuted_ones_do_not():
    confirmed = Hypothesis(rank=1, statement="a", status="confirmed", evidence_ids=EV)
    refuted = Hypothesis(rank=2, statement="b", status="refuted", evidence_ids=EV)
    unverified = Hypothesis(rank=3, statement="c", status="unverified", evidence_ids=["ev:x"])
    report = _report(findings=[_finding()], hypotheses=[confirmed, refuted, unverified])
    score = score_report("demo", report, Expectation(component="alb"))
    assert score.claims_total == 2  # the finding + the confirmed hypothesis
    assert score.claims_valid == 2


def test_forbidden_term_in_finding_is_a_hit():
    report = _report(findings=[_finding(detail="the worker was OOMKilled")])
    score = score_report(
        "demo", report, Expectation(component="alb", forbidden_terms=("OOMKilled",))
    )
    assert score.forbidden_hits == ("OOMKilled",)


def test_forbidden_term_in_confirmed_hypothesis_is_a_hit():
    hyp = Hypothesis(rank=1, statement="it was OOMKilled", status="confirmed", evidence_ids=EV)
    report = _report(hypotheses=[hyp])
    score = score_report("demo", report, Expectation(forbidden_terms=("OOMKilled",)))
    assert score.forbidden_hits == ("OOMKilled",)


def test_forbidden_term_in_unverified_hypothesis_is_not_a_hit():
    hyp = Hypothesis(rank=2, statement="it was OOMKilled", status="unverified")
    report = _report(findings=[_finding()], hypotheses=[hyp])
    score = score_report(
        "demo", report, Expectation(component="alb", forbidden_terms=("OOMKilled",))
    )
    assert score.forbidden_hits == ()


def test_abstention_expectation_met():
    report = _report(abstained=True)
    score = score_report("demo", report, Expectation(abstain=True))
    assert score.abstention_ok
    assert score.abstained


def test_abstention_expected_but_findings_reported_fails():
    report = _report(findings=[_finding()])
    score = score_report("demo", report, Expectation(component="alb", abstain=True))
    assert not score.abstention_ok


def test_abstention_not_expected_but_report_abstains_fails():
    report = _report(abstained=True)
    score = score_report("demo", report, Expectation(component="alb", fault_class="X"))
    assert not score.abstention_ok
    assert not score.component_ok
    assert not score.fault_class_ok


# ------------------------------------------------------------------ summary


def test_summary_aggregates_accuracy_and_abstention_rate():
    good = score_report(
        "a",
        _report(findings=[_finding()]),
        Expectation(component="alb", fault_class="TargetHealthDegraded"),
    )
    bad = score_report(
        "b", _report(findings=[_finding(service="ecs")]), Expectation(component="alb")
    )
    abstained = score_report("c", _report(abstained=True), Expectation(abstain=True))
    summary = summarize([good, bad, abstained], determinism_ok=True)
    assert summary.component_accuracy == 2 / 3
    assert summary.fault_class_accuracy == 2 / 3
    assert summary.abstention_rate == 1 / 3
    assert summary.determinism_ok
    assert len(summary.scores) == 3


def test_summary_with_no_claims_reports_full_evidence_validity():
    summary = summarize([score_report("a", _report(abstained=True), Expectation(abstain=True))])
    assert summary.evidence_validity_pct == 100.0
    assert summary.determinism_ok is False


# ------------------------------------------------------------------- replay


def test_fingerprint_ignores_run_local_fields_but_detects_changes():
    a = _report(findings=[_finding()])
    b = _report(findings=[_finding()])
    b = b.model_copy(
        update={
            "created_at": datetime(2020, 1, 1, tzinfo=UTC),
            "investigation_id": "inv:s3_access_denied:20200101T000000z0000",
        }
    )
    assert report_fingerprint(a) == report_fingerprint(b)
    assert same_report(a, b)

    c = _report(findings=[_finding(cause_token="AccessDenied")])
    assert not same_report(a, c)


# ------------------------------------------------------------------- golden


def test_load_golden_parses_the_repo_file():
    path = Path(__file__).resolve().parents[2] / "evals" / "golden.yaml"
    scenarios = load_golden(path)
    names = [s.name for s in scenarios]
    assert {"s3_access_denied", "poisoned_logs", "insufficient_evidence"} <= set(names)
    poisoned = next(s for s in scenarios if s.name == "poisoned_logs")
    assert isinstance(poisoned, GoldenScenario)
    assert poisoned.expectation.forbidden_terms == ("OOMKilled",)
    absten = next(s for s in scenarios if s.name == "insufficient_evidence")
    assert absten.expectation.abstain is True
    assert all(s.question for s in scenarios)
