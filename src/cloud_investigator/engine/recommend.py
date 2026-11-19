"""Recommendations — culprit version, rollback, recovery verification.

The mezmo/aura pattern (PLAN Phase 2 item 7): a recommendation names what to
change, what to roll back to, and how to prove recovery. Everything here is
deterministic extraction and templates — the model's prose stays in
`Finding.recommendation`; this layer makes the plan checkable.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

from ..contracts import Citation, EvidenceItem, Finding

_VERSION = re.compile(r"\bv(\d+)\b", re.IGNORECASE)


class RecommendationPlan(BaseModel):
    """Structured recommendation rendered into the report's plain strings."""

    action: str
    culprit_version: str = ""
    rollback_steps: list[str] = Field(default_factory=list)
    verification_steps: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)


def _culprit_version(finding: Finding, evidence: list[EvidenceItem]) -> str:
    """First explicit `vN` mention in the finding text or its evidence."""
    for text in [finding.detail, *(item.excerpt for item in evidence)]:
        match = _VERSION.search(text or "")
        if match:
            return match.group(0).lower()
    return ""


def _target(finding: Finding) -> str:
    if finding.resource is not None and finding.resource.display:
        return finding.resource.display
    return "the affected resource"


def build_plans(findings: list[Finding], evidence: list[EvidenceItem]) -> list[RecommendationPlan]:
    """One plan per distinct cause, in finding order (first occurrence wins)."""
    by_id = {item.id: item for item in evidence}
    seen: set[str] = set()
    plans: list[RecommendationPlan] = []

    for finding in findings:
        key = finding.cause_token or finding.title
        if key in seen:
            continue
        seen.add(key)

        cited = [by_id[i] for i in finding.evidence_ids if i in by_id]
        target = _target(finding)
        version = _culprit_version(finding, cited)

        if version:
            rollback = [
                f"restore the previous version of {target} (roll back from {version} "
                "to the last known-good revision)",
                f"re-apply that revision to {target} and confirm it is active",
            ]
        else:
            rollback = [
                f"restore the last known-good configuration of {target}",
                f"confirm the restored configuration is active on {target}",
            ]

        plans.append(
            RecommendationPlan(
                action=finding.recommendation
                or f"remediate {finding.cause_token or finding.title} on {target}",
                culprit_version=version,
                rollback_steps=rollback,
                verification_steps=[
                    f"re-run the failing operation against {target}",
                    "verify the success signal in logs and audit rows for the same window",
                    "compare the error rate with the pre-incident baseline",
                ],
                evidence_ids=list(finding.evidence_ids),
                citations=list(finding.citations),
            )
        )
    return plans


def render(plan: RecommendationPlan) -> str:
    """Plain-text form for `InvestigationReport.recommendations`."""
    culprit = f" (culprit: {plan.culprit_version})" if plan.culprit_version else ""
    action = plan.action.strip().rstrip(".")
    rollback = "; ".join(plan.rollback_steps)
    verify = "; ".join(plan.verification_steps)
    return f"{action}{culprit}. Rollback: {rollback}. Verify: {verify}."
