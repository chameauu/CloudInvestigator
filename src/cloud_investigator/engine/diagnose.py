"""Diagnose — competing root-cause candidates with a deterministic fallback.

`PLAN.md` Phase 2 item 6: at least two competing `root_cause_candidates`, each
carrying supporting/contradicting evidence and `required_next_evidence`; a
deterministic fallback when the model fails; deterministic id checks.

Division of labour, per the project's non-negotiables:
- The model (when one is configured) *proposes* candidates — it never mints
  evidence ids and never decides a claim is confirmed by itself.
- Code validates: unknown ids are stripped, a confirmed candidate without
  surviving evidence is demoted, and fewer than two candidates triggers the
  fallback.
- The fallback is pure Python over the evidence, so an offline run still
  produces a useful, honest pair of competing explanations.
"""

from __future__ import annotations

from typing import Literal, Protocol

from pydantic import BaseModel, Field

from ..contracts import EvidenceItem, Finding, Hypothesis

CandidateStatus = Literal["confirmed", "refuted", "unverified"]

#: Deterministic competing explanations per cause class. Code, not model output:
#: the point is that a second hypothesis always exists and is grounded.
COMPETING_BY_CAUSE: dict[str, str] = {
    "AccessDenied": (
        "Transient credential expiry or IAM policy propagation lag, "
        "rather than a deliberate policy change"
    ),
    "ConnectionPoolExhausted": (
        "Upstream latency holding connections open, rather than a client-side pool-size limit"
    ),
    "TargetHealthDegraded": (
        "Application-level 5xx emitted by healthy targets, rather than a "
        "health-check configuration fault"
    ),
    "TargetPortMismatch": (
        "The new tasks fail to start, so the target group has no backend, "
        "rather than a port the balancer cannot reach"
    ),
    "OOMKilled": ("A one-off load spike or memory leak, rather than an undersized memory limit"),
    "Throttling": (
        "A retry storm amplifying one slow dependency, rather than an account-level quota"
    ),
}

DEFAULT_COMPETING = (
    "A different component on the request path is the trigger, not the "
    "component that reported the symptom"
)

_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}


class RootCauseCandidate(BaseModel):
    """One competing explanation. `supporting_ids` must resolve to state evidence."""

    statement: str
    status: CandidateStatus = "unverified"
    cause_token: str = ""
    supporting_ids: list[str] = Field(default_factory=list)
    contradicting_ids: list[str] = Field(default_factory=list)
    required_next_evidence: list[str] = Field(
        default_factory=list,
        description="Concrete read-only checks that would confirm or refute this candidate",
    )
    reasoning: str = ""


class DiagnoseResult(BaseModel):
    candidates: list[RootCauseCandidate] = Field(default_factory=list)
    used_fallback: bool = False
    rejected_ids: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class Diagnoser(Protocol):
    """Anything that can propose candidates: cassette stand-in or a real model."""

    def diagnose(
        self, question: str, evidence: list[EvidenceItem], findings: list[Finding]
    ) -> list[RootCauseCandidate]: ...


def _next_evidence(evidence: list[EvidenceItem], cause_token: str) -> list[str]:
    """Concrete read-only checks a human (or the next phase) would run."""
    kinds = {e.kind for e in evidence}
    resources = sorted({e.resource.display for e in evidence if e.resource is not None})
    target = resources[0] if resources else "the affected resource"
    checks: list[str] = []
    if "cloudwatch_logs" not in kinds:
        checks.append(f"read the service logs around the first failure on {target}")
    if "sql" not in kinds:
        checks.append(f"read the audit/config history rows for {target}")
    checks.append(f"describe the current state (versions, policy revision) of {target}")
    if cause_token:
        checks.append(f"compare the {cause_token} signal against the previous healthy window")
    return checks[:3]


def deterministic_candidates(
    evidence: list[EvidenceItem], findings: list[Finding]
) -> list[RootCauseCandidate]:
    """Build ≥2 competing candidates from evidence alone — no model involved."""
    valid = {e.id for e in evidence}
    cited: set[str] = set()
    candidates: list[RootCauseCandidate] = []

    ranked_findings = sorted(
        findings, key=lambda f: (_SEVERITY_ORDER.get(f.severity, 9), f.title.lower())
    )
    for finding in ranked_findings[:2]:
        ids = [i for i in finding.evidence_ids if i in valid]
        cited.update(ids)
        candidates.append(
            RootCauseCandidate(
                statement=finding.title,
                status="confirmed" if ids else "unverified",
                cause_token=finding.cause_token,
                supporting_ids=ids,
                reasoning=finding.detail,
            )
        )

    cause_token = candidates[0].cause_token if candidates else ""
    competitor_statement = COMPETING_BY_CAUSE.get(cause_token, DEFAULT_COMPETING)

    if len(candidates) < 2:
        # No usable finding: cluster the evidence by (kind, resource) so the two
        # candidates reflect what was actually observed, strongest cluster first.
        clusters: dict[tuple[str, str], list[EvidenceItem]] = {}
        for item in evidence:
            key = (item.kind, item.resource.display if item.resource else "")
            clusters.setdefault(key, []).append(item)
        ordered = sorted(clusters.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        for (kind, resource), items in ordered[:2]:
            if any(c.statement.startswith(f"{kind} signal") for c in candidates):
                continue
            where = f" on {resource}" if resource else ""
            candidates.append(
                RootCauseCandidate(
                    statement=f"{kind} signal{where} indicates the fault surface",
                    status="unverified",
                    supporting_ids=[e.id for e in items][:5],
                    required_next_evidence=_next_evidence(evidence, ""),
                    reasoning="observed evidence cluster; no finding has confirmed it yet",
                )
            )

    # Always add one genuinely competing explanation.
    uncited = [e.id for e in evidence if e.id not in cited]
    candidates.append(
        RootCauseCandidate(
            statement=competitor_statement,
            status="unverified",
            supporting_ids=uncited[:5],
            required_next_evidence=_next_evidence(evidence, cause_token),
            reasoning="competing explanation that the current evidence does not exclude",
        )
    )

    if len(candidates) < 2:
        candidates.insert(
            0,
            RootCauseCandidate(
                statement="Insufficient evidence — no probe returned usable data",
                status="unverified",
                required_next_evidence=[
                    "connect at least one read-only probe for the affected service"
                ],
            ),
        )
    return candidates


def _validate(
    candidates: list[RootCauseCandidate], valid_ids: set[str]
) -> tuple[list[RootCauseCandidate], list[str]]:
    """Strip ids that do not exist in state; demote unsupported confirmations."""
    rejected: list[str] = []
    kept: list[RootCauseCandidate] = []
    for candidate in candidates:

        def keep(ids: list[str]) -> list[str]:
            good = [i for i in ids if i in valid_ids]
            rejected.extend(i for i in ids if i not in valid_ids)
            return good

        supporting = keep(candidate.supporting_ids)
        contradicting = keep(candidate.contradicting_ids)
        status = candidate.status
        reasoning = candidate.reasoning
        if status == "confirmed" and not supporting:
            status = "unverified"
            reasoning = f"{reasoning} [demoted: confirmed without resolvable evidence]".strip()
        kept.append(
            candidate.model_copy(
                update={
                    "supporting_ids": supporting,
                    "contradicting_ids": contradicting,
                    "status": status,
                    "reasoning": reasoning,
                }
            )
        )
    # dedupe, keep first-seen order
    seen: list[str] = []
    for item in rejected:
        if item not in seen:
            seen.append(item)
    return kept, seen


def diagnose(
    question: str,
    evidence: list[EvidenceItem],
    findings: list[Finding],
    diagnoser: Diagnoser | None = None,
) -> DiagnoseResult:
    """Validate the model's candidates, or fall back deterministically."""
    valid_ids = {e.id for e in evidence}
    notes: list[str] = []

    if diagnoser is not None:
        try:
            proposed = diagnoser.diagnose(question, evidence, findings)
        except Exception as exc:  # noqa: BLE001 - the fallback is the point
            notes.append(
                f"diagnoser failed: {type(exc).__name__}: {exc}; using deterministic fallback"
            )
        else:
            if len(proposed) < 2:
                notes.append(
                    f"diagnoser returned {len(proposed)} candidate(s); at least 2 competing "
                    "candidates required — using deterministic fallback"
                )
            else:
                kept, rejected = _validate(list(proposed), valid_ids)
                return DiagnoseResult(
                    candidates=kept,
                    used_fallback=False,
                    rejected_ids=rejected,
                    notes=notes,
                )

    fallback, rejected = _validate(deterministic_candidates(evidence, findings), valid_ids)
    return DiagnoseResult(
        candidates=fallback, used_fallback=True, rejected_ids=rejected, notes=notes
    )


def to_hypotheses(result: DiagnoseResult) -> list[Hypothesis]:
    """Map candidates onto the pinned report contract (rank = display order)."""
    hypotheses: list[Hypothesis] = []
    for rank, candidate in enumerate(result.candidates, start=1):
        arguments_for: list[str] = []
        if candidate.reasoning:
            arguments_for.append(candidate.reasoning)
        if candidate.supporting_ids:
            arguments_for.append("supported by: " + ", ".join(candidate.supporting_ids))
        arguments_against: list[str] = []
        if candidate.contradicting_ids:
            arguments_against.append("contradicted by: " + ", ".join(candidate.contradicting_ids))
        arguments_against.extend(f"needs: {item}" for item in candidate.required_next_evidence)
        if result.used_fallback:
            arguments_against.append("proposed by the deterministic fallback, not a model")
        hypotheses.append(
            Hypothesis(
                rank=rank,
                statement=candidate.statement,
                status=candidate.status,
                arguments_for=arguments_for,
                arguments_against=arguments_against,
                evidence_ids=list(candidate.supporting_ids),
            )
        )
    return hypotheses
