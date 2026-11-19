"""Diagnose contract tests — ≥2 competing candidates, deterministic fallback.

The LLM (or its cassette stand-in) may propose candidates; code validates them.
If the model fails, returns junk, or returns fewer than two competitors, the
deterministic fallback takes over. A confirmed candidate must cite evidence
that exists in state, otherwise it is demoted.
"""

from __future__ import annotations

from cloud_investigator.contracts import EvidenceItem, Finding, ResourceRef
from cloud_investigator.engine.diagnose import (
    DiagnoseResult,
    RootCauseCandidate,
    deterministic_candidates,
    diagnose,
    to_hypotheses,
)

BUCKET = ResourceRef(provider="aws", region="eu-west-1", service="s3", name="qa-data-lake")
ROLE = ResourceRef(
    provider="aws", region="eu-west-1", service="iam", name="role/qa-ingest-uploader"
)


def _evidence() -> list[EvidenceItem]:
    return [
        EvidenceItem(
            id="ev:s3_access_denied:sql:1",
            kind="sql",
            resource=BUCKET,
            query="SELECT actor, decision FROM iam_audit_events",
            excerpt="actor=role/qa-ingest-uploader decision=DENY policy_id=ExplicitDenyStmt-v3",
        ),
        EvidenceItem(
            id="ev:s3_access_denied:cloudwatch_logs:1",
            kind="cloudwatch_logs",
            resource=ROLE,
            query="filter_log_events filter=AccessDenied",
            excerpt="ERROR UploadTask failed — AccessDenied on GetObject (bucket=qa-data-lake)",
        ),
        EvidenceItem(
            id="ev:s3_access_denied:cloudwatch_logs:2",
            kind="cloudwatch_logs",
            resource=ROLE,
            query="filter_log_events filter=AccessDenied",
            excerpt="INFO config watcher detected s3:PutBucketPolicy (new policy version v3)",
        ),
    ]


def _finding(ids: list[str]) -> Finding:
    return Finding(
        severity="high",
        title="Explicit Deny blocks s3:GetObject",
        detail="Bucket policy v3 denies the uploader role.",
        resource=BUCKET,
        cause_token="AccessDenied",
        confidence="high",
        evidence_ids=ids,
    )


class _StubDiagnoser:
    """Deterministic stand-in for an LLM analyst."""

    def __init__(self, candidates: list[RootCauseCandidate] | None = None, boom: bool = False):
        self._candidates = candidates or []
        self._boom = boom
        self.calls = 0

    def diagnose(self, question, evidence, findings):
        self.calls += 1
        if self._boom:
            raise RuntimeError("provider timeout")
        return list(self._candidates)


def test_deterministic_fallback_produces_two_competitors_with_findings():
    evidence = _evidence()
    candidates = deterministic_candidates(
        evidence, [_finding(["ev:s3_access_denied:sql:1", "ev:s3_access_denied:cloudwatch_logs:1"])]
    )
    assert len(candidates) >= 2
    primary, competitor = candidates[0], candidates[1]
    assert primary.status == "confirmed"
    assert primary.cause_token == "AccessDenied"
    assert "ev:s3_access_denied:sql:1" in primary.supporting_ids
    assert competitor.status in ("unverified", "refuted")
    assert competitor.statement != primary.statement
    assert competitor.required_next_evidence, "a competitor must say what would settle it"


def test_deterministic_fallback_without_findings_clusters_evidence():
    candidates = deterministic_candidates(_evidence(), [])
    assert len(candidates) >= 2
    assert all(c.status == "unverified" for c in candidates)
    ids = {i for c in candidates for i in c.supporting_ids}
    assert ids <= {e.id for e in _evidence()}


def test_deterministic_fallback_with_nothing_still_returns_two_candidates():
    candidates = deterministic_candidates([], [])
    assert len(candidates) == 2
    assert all(not c.supporting_ids for c in candidates)
    assert all(c.status == "unverified" for c in candidates)


def test_no_diagnoser_uses_fallback_and_says_so():
    result = diagnose("why?", _evidence(), [_finding(["ev:s3_access_denied:sql:1"])])
    assert isinstance(result, DiagnoseResult)
    assert result.used_fallback is True
    assert len(result.candidates) >= 2


def test_working_diagnoser_is_used_verbatim():
    stub = _StubDiagnoser(
        [
            RootCauseCandidate(
                statement="Policy v3 explicit Deny",
                status="confirmed",
                cause_token="AccessDenied",
                supporting_ids=["ev:s3_access_denied:sql:1"],
            ),
            RootCauseCandidate(
                statement="KMS key policy denies decrypt",
                status="unverified",
                supporting_ids=[],
                required_next_evidence=["describe the bucket KMS key policy"],
            ),
        ]
    )
    result = diagnose("why?", _evidence(), [], diagnoser=stub)
    assert result.used_fallback is False
    assert stub.calls == 1
    assert [c.statement for c in result.candidates][0] == "Policy v3 explicit Deny"


def test_diagnoser_failure_falls_back_with_verbatim_reason():
    result = diagnose("why?", _evidence(), [], diagnoser=_StubDiagnoser(boom=True))
    assert result.used_fallback is True
    assert any("provider timeout" in n for n in result.notes)
    assert len(result.candidates) >= 2


def test_diagnoser_returning_one_candidate_falls_back():
    stub = _StubDiagnoser([RootCauseCandidate(statement="only one idea", status="unverified")])
    result = diagnose("why?", _evidence(), [], diagnoser=stub)
    assert result.used_fallback is True
    assert any("at least 2" in n for n in result.notes)


def test_unknown_evidence_ids_are_stripped_and_confirmed_is_demoted():
    stub = _StubDiagnoser(
        [
            RootCauseCandidate(
                statement="ghost cause",
                status="confirmed",
                supporting_ids=["ev:not-real:sql:9"],
            ),
            RootCauseCandidate(statement="other idea", status="unverified"),
        ]
    )
    result = diagnose("why?", _evidence(), [], diagnoser=stub)
    assert "ev:not-real:sql:9" in result.rejected_ids
    assert result.candidates[0].supporting_ids == []
    assert result.candidates[0].status == "unverified"


def test_to_hypotheses_maps_candidates_with_arguments():
    result = diagnose("why?", _evidence(), [_finding(["ev:s3_access_denied:sql:1"])])
    hypotheses = to_hypotheses(result)
    assert hypotheses
    assert [h.rank for h in hypotheses] == list(range(1, len(hypotheses) + 1))
    assert hypotheses[0].status == "confirmed"
    assert hypotheses[0].evidence_ids
    assert hypotheses[0].arguments_for
    competitor = hypotheses[1]
    assert competitor.arguments_against, "competitor states what would refute it"


def test_diagnose_is_deterministic():
    a = diagnose("why?", _evidence(), [_finding(["ev:s3_access_denied:sql:1"])])
    b = diagnose("why?", _evidence(), [_finding(["ev:s3_access_denied:sql:1"])])
    assert a.model_dump() == b.model_dump()


def test_candidate_model_is_json_serializable():
    payload = diagnose("why?", _evidence(), []).model_dump()
    assert isinstance(payload["candidates"], list)
    assert payload["used_fallback"] in (True, False)
