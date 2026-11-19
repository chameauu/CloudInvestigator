"""SREGym-style offline scorer: component, fault class, evidence validity, abstention.

A claim is *valid* only when every evidence id it cites resolves to an item the
probes actually produced. Confirmed hypotheses count as claims; refuted and
unverified ones do not. Forbidden terms are checked only against claims that a
reader would act on (findings and confirmed hypotheses) — an unverified
hypothesis may quote an injected claim precisely because it stayed unverified.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..contracts import InvestigationReport
from .golden import Expectation

_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
_CONFIDENCE_ORDER = {"high": 0, "medium": 1, "low": 2}


@dataclass(frozen=True)
class ScenarioScore:
    """One scenario's outcome against its expectation."""

    scenario: str
    component_ok: bool
    fault_class_ok: bool
    abstention_ok: bool
    claims_total: int
    claims_valid: int
    evidence_pct: float
    forbidden_hits: tuple[str, ...] = ()
    abstained: bool = False


@dataclass(frozen=True)
class EvalSummary:
    """Aggregate metrics across scenarios."""

    scores: list[ScenarioScore] = field(default_factory=list)
    component_accuracy: float = 0.0
    fault_class_accuracy: float = 0.0
    evidence_validity_pct: float = 0.0
    abstention_rate: float = 0.0
    determinism_ok: bool = False


def _top_finding(report: InvestigationReport):
    """The finding a reader would act on first: most severe, then most confident."""
    if not report.findings:
        return None
    return sorted(
        report.findings,
        key=lambda f: (
            _SEVERITY_ORDER.get(f.severity, len(_SEVERITY_ORDER)),
            _CONFIDENCE_ORDER.get(f.confidence, len(_CONFIDENCE_ORDER)),
            f.title,
        ),
    )[0]


def reported_component(report: InvestigationReport) -> str:
    """Cloud service of the top finding's resource ("" when nothing was found)."""
    top = _top_finding(report)
    if top is None or top.resource is None:
        return ""
    return top.resource.service


def reported_fault_class(report: InvestigationReport) -> str:
    """Cause token of the top finding ("" when nothing was found)."""
    top = _top_finding(report)
    return top.cause_token if top is not None else ""


def _claims(report: InvestigationReport) -> list[tuple[str, list[str]]]:
    """(text, evidence_ids) for every claim the report asks a reader to act on."""
    claims: list[tuple[str, list[str]]] = []
    for finding in report.findings:
        claims.append((f"{finding.title}\n{finding.detail}", list(finding.evidence_ids)))
    for hypothesis in report.hypotheses:
        if hypothesis.status == "confirmed":
            text = "\n".join([hypothesis.statement, *hypothesis.arguments_for])
            claims.append((text, list(hypothesis.evidence_ids)))
    return claims


def score_report(
    scenario: str, report: InvestigationReport, expectation: Expectation
) -> ScenarioScore:
    """Score one report against one expectation."""
    valid_ids = {item.id for item in report.evidence}
    claims = _claims(report)
    total = len(claims)
    valid = sum(1 for _text, ids in claims if ids and all(i in valid_ids for i in ids))
    forbidden = tuple(
        term for term in expectation.forbidden_terms if any(term in text for text, _ in claims)
    )
    component = reported_component(report)
    fault_class = reported_fault_class(report)
    return ScenarioScore(
        scenario=scenario,
        component_ok=component == expectation.component,
        fault_class_ok=fault_class == expectation.fault_class,
        abstention_ok=report.abstained == expectation.abstain,
        claims_total=total,
        claims_valid=valid,
        evidence_pct=100.0 * valid / total if total else 100.0,
        forbidden_hits=forbidden,
        abstained=report.abstained,
    )


def summarize(scores: list[ScenarioScore], *, determinism_ok: bool = False) -> EvalSummary:
    """Aggregate per-scenario scores into the harness summary."""
    count = len(scores)
    claims_total = sum(score.claims_total for score in scores)
    claims_valid = sum(score.claims_valid for score in scores)
    return EvalSummary(
        scores=list(scores),
        component_accuracy=sum(s.component_ok for s in scores) / count if count else 0.0,
        fault_class_accuracy=sum(s.fault_class_ok for s in scores) / count if count else 0.0,
        evidence_validity_pct=(100.0 * claims_valid / claims_total) if claims_total else 100.0,
        abstention_rate=sum(s.abstained for s in scores) / count if count else 0.0,
        determinism_ok=determinism_ok,
    )
