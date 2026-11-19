"""Recommendation contract tests — culprit version, rollback, verification.

PLAN Phase 2 item 7 (mezmo/aura pattern): a recommendation is not just "fix
it". It names the culprit version when the evidence carries one, says how to
roll back, and says how to verify recovery — all deterministic.
"""

from __future__ import annotations

from cloud_investigator.contracts import EvidenceItem, Finding, ResourceRef
from cloud_investigator.engine.recommend import (
    RecommendationPlan,
    build_plans,
    render,
)

BUCKET = ResourceRef(provider="aws", region="eu-west-1", service="s3", name="qa-data-lake")


def _finding() -> Finding:
    return Finding(
        severity="high",
        title="Explicit Deny blocks s3:GetObject",
        detail="Bucket policy v3 denies the uploader role.",
        resource=BUCKET,
        cause_token="AccessDenied",
        confidence="high",
        evidence_ids=["ev:s3_access_denied:sql:1"],
        recommendation="Remove the explicit Deny statement for role/qa-ingest-uploader.",
    )


def _evidence() -> list[EvidenceItem]:
    return [
        EvidenceItem(
            id="ev:s3_access_denied:cloudwatch_logs:2",
            kind="cloudwatch_logs",
            resource=BUCKET,
            query="filter_log_events",
            excerpt=(
                "INFO config watcher detected s3:PutBucketPolicy on "
                "arn:aws:s3:::qa-data-lake (new policy version v3)"
            ),
        ),
        EvidenceItem(
            id="ev:s3_access_denied:sql:1",
            kind="sql",
            resource=BUCKET,
            query="SELECT decision FROM iam_audit_events",
            excerpt="decision=DENY policy_id=ExplicitDenyStmt-bucketpolicy-v3",
        ),
    ]


def test_build_plans_extracts_culprit_version_and_verification():
    plans = build_plans([_finding()], _evidence())
    assert len(plans) == 1
    plan = plans[0]
    assert isinstance(plan, RecommendationPlan)
    assert plan.culprit_version == "v3"
    assert plan.action.startswith("Remove the explicit Deny")
    assert any("previous version" in step.lower() for step in plan.rollback_steps)
    assert any(
        "re-run" in step.lower() or "verify" in step.lower() for step in plan.verification_steps
    )
    assert plan.evidence_ids == ["ev:s3_access_denied:sql:1"]


def test_render_includes_the_three_parts():
    text = render(build_plans([_finding()], _evidence())[0])
    lowered = text.lower()
    assert "culprit" in lowered
    assert "rollback" in lowered
    assert "verify" in lowered


def test_no_version_in_evidence_falls_back_to_last_known_good():
    plain = _finding().model_copy(update={"detail": "Bucket policy denies the uploader role."})
    plan = build_plans([plain], [])[0]
    assert plan.culprit_version == ""
    assert any("last known-good" in step.lower() for step in plan.rollback_steps)


def test_no_findings_means_no_plans():
    assert build_plans([], _evidence()) == []


def test_duplicate_cause_tokens_are_merged():
    plans = build_plans([_finding(), _finding()], _evidence())
    assert len(plans) == 1


def test_plans_are_json_serializable():
    payload = build_plans([_finding()], _evidence())[0].model_dump()
    assert payload["culprit_version"] == "v3"
    assert isinstance(payload["verification_steps"], list)
