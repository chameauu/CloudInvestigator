"""Elimination contract tests — hypotheses must visibly die as evidence lands.

Deterministic: each event is caused by a specific evidence arrival, ordered by
(time, id). No model output can suppress or invent an elimination.
"""

from __future__ import annotations

from cloud_investigator.contracts import EvidenceItem, TimeRange
from cloud_investigator.engine.diagnose import RootCauseCandidate
from cloud_investigator.engine.elimination import EliminationEvent, eliminate


def _ev(i: int, ts: str, excerpt: str = "row") -> EvidenceItem:
    return EvidenceItem(
        id=f"ev:s3_access_denied:sql:{i}",
        kind="sql",
        ts=TimeRange(start=ts, end=ts, label=""),
        excerpt=excerpt,
    )


def test_confirming_evidence_confirms_at_last_supporting_id():
    evidence = [_ev(1, "2026-11-18T09:00:00Z"), _ev(2, "2026-11-18T09:05:00Z")]
    candidates = [
        RootCauseCandidate(
            statement="Policy v3 deny",
            supporting_ids=["ev:s3_access_denied:sql:1", "ev:s3_access_denied:sql:2"],
        )
    ]
    final, events = eliminate(candidates, evidence)
    assert final[0].status == "confirmed"
    assert len(events) == 1
    event = events[0]
    assert isinstance(event, EliminationEvent)
    assert (event.from_status, event.to_status) == ("unverified", "confirmed")
    assert event.seq == 2  # arrives with the second item


def test_contradicting_evidence_refutes_at_first_arrival():
    evidence = [_ev(1, "2026-11-18T09:00:00Z"), _ev(2, "2026-11-18T09:05:00Z")]
    candidates = [
        RootCauseCandidate(
            statement="Network path fault",
            contradicting_ids=["ev:s3_access_denied:sql:1"],
        )
    ]
    final, events = eliminate(candidates, evidence)
    assert final[0].status == "refuted"
    assert len(events) == 1
    assert events[0].to_status == "refuted"
    assert events[0].seq == 1
    assert events[0].evidence_ids == ["ev:s3_access_denied:sql:1"]


def test_missing_supporting_evidence_leaves_candidate_unverified():
    candidates = [RootCauseCandidate(statement="unsupported", supporting_ids=["ev:gone:sql:9"])]
    final, events = eliminate(candidates, [_ev(1, "2026-11-18T09:00:00Z")])
    assert final[0].status == "unverified"
    assert events == []


def test_events_are_ordered_by_arrival():
    evidence = [_ev(1, "2026-11-18T09:00:00Z"), _ev(2, "2026-11-18T09:10:00Z")]
    candidates = [
        RootCauseCandidate(statement="late confirm", supporting_ids=["ev:s3_access_denied:sql:2"]),
        RootCauseCandidate(
            statement="early refute", contradicting_ids=["ev:s3_access_denied:sql:1"]
        ),
    ]
    _, events = eliminate(candidates, evidence)
    assert [e.seq for e in events] == [1, 2]
    assert [e.to_status for e in events] == ["refuted", "confirmed"]


def test_no_candidates_no_events():
    final, events = eliminate([], [_ev(1, "2026-11-18T09:00:00Z")])
    assert final == [] and events == []


def test_elimination_is_deterministic_and_serializable():
    evidence = [_ev(1, "2026-11-18T09:00:00Z"), _ev(2, "2026-11-18T09:10:00Z")]
    candidates = [
        RootCauseCandidate(statement="a", supporting_ids=["ev:s3_access_denied:sql:2"]),
        RootCauseCandidate(statement="b", contradicting_ids=["ev:s3_access_denied:sql:1"]),
    ]
    first = eliminate(candidates, evidence)
    second = eliminate(candidates, evidence)
    assert [c.model_dump() for c in first[0]] == [c.model_dump() for c in second[0]]
    assert [e.model_dump() for e in first[1]] == [e.model_dump() for e in second[1]]
