"""Elimination — competing hypotheses visibly die (or graduate) as evidence lands.

Deterministic by construction: an evidence item that contradicts a candidate
refutes it at the moment it arrives; the last supporting item confirms one.
The SSE stream shows the same events the report accounts for — no model call
can suppress an elimination, and none can invent one.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..contracts import EvidenceItem
from .diagnose import RootCauseCandidate

_EXCERPT_CHARS = 160


class EliminationEvent(BaseModel):
    """One hypothesis status transition caused by one evidence arrival."""

    seq: int = Field(description="1-based arrival index of the deciding evidence")
    candidate_rank: int
    statement: str
    from_status: str = "unverified"
    to_status: str
    reason: str
    evidence_ids: list[str] = Field(default_factory=list)


def _arrival_key(item: EvidenceItem) -> tuple[str, str]:
    """Stable ordering without trusting tz-awareness across sources."""
    stamp = item.ts.start.isoformat() if item.ts is not None else ""
    return (stamp, item.id)


def eliminate(
    candidates: list[RootCauseCandidate], evidence: list[EvidenceItem]
) -> tuple[list[RootCauseCandidate], list[EliminationEvent]]:
    """Replay evidence in arrival order; emit transitions and update statuses."""
    ordered = sorted(evidence, key=_arrival_key)
    position = {item.id: index + 1 for index, item in enumerate(ordered)}
    by_id = {item.id: item for item in ordered}

    final: list[RootCauseCandidate] = []
    events: list[EliminationEvent] = []

    for rank, candidate in enumerate(candidates, start=1):
        support = [i for i in candidate.supporting_ids if i in position]
        against = [i for i in candidate.contradicting_ids if i in position]
        status = "unverified"
        reasoning = candidate.reasoning

        if against:
            first = min(against, key=lambda i: position[i])
            status = "refuted"
            reason = f"contradicted by {first}: {by_id[first].excerpt[:_EXCERPT_CHARS]}"
            reasoning = f"{reasoning} [{reason}]".strip()
            events.append(
                EliminationEvent(
                    seq=position[first],
                    candidate_rank=rank,
                    statement=candidate.statement,
                    to_status="refuted",
                    reason=reason,
                    evidence_ids=[first],
                )
            )
        elif support and all(i in position for i in support):
            last = max(support, key=lambda i: position[i])
            status = "confirmed"
            reason = f"all supporting evidence present (deciding item: {last})"
            reasoning = f"{reasoning} [{reason}]".strip()
            events.append(
                EliminationEvent(
                    seq=position[last],
                    candidate_rank=rank,
                    statement=candidate.statement,
                    to_status="confirmed",
                    reason=reason,
                    evidence_ids=list(support),
                )
            )

        final.append(candidate.model_copy(update={"status": status, "reasoning": reasoning}))

    events.sort(key=lambda e: (e.seq, e.candidate_rank))
    return final, events
