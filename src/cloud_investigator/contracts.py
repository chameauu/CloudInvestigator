"""Shared typed contracts — every node boundary speaks these schemas.

House rules (derived from the inspiration research, docs/inspiration-and-blueprint.md):
1. No free-text handoffs between nodes — Pydantic at every boundary.
2. Evidence is minted ONLY by source adapters (tools). LLM nodes reference
   evidence ids; they can never invent evidence. A causal claim without a
   resolvable evidence id is a Hypothesis, not a Finding (verify node demotes it).
3. Read-only by construction: tools are scoped (time range + limits) and every
   probe records what was actually executed (ToolCallRecord) so the report can
   show its own coverage honestly.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

Severity = Literal["critical", "high", "medium", "low", "info"]
Confidence = Literal["high", "medium", "low"]

SourceKind = Literal[
    "cloudwatch_logs",      # CloudWatch Logs Insights / filter_log_events
    "cloudwatch_metrics",    # GetMetricData
    "resource_state",        # describe_*/list_*/get_* across services
    "sql",                   # authorized read-only database
    "runbook",               # RAG over runbooks / past postmortems
    "catalog",               # service catalog / topology map
    "web",                   # optional external research
]


class TimeRange(BaseModel):
    """All data queries are windowed — no unbounded API calls."""
    start: datetime
    end: datetime
    label: str = ""


class ResourceRef(BaseModel):
    """A resolved cloud resource. Deterministic: resolved from the service
    catalog or a describe call — never invented by an LLM node."""
    provider: Literal["aws", "azure", "gcp", "k8s", "sql", "unknown"] = "aws"
    account_id: str = ""
    region: str = ""
    service: str = ""          # rds, ec2, alb, s3, ecs, lambda...
    resource_type: str = ""     # db_instance, load_balancer, bucket...
    resource_id: str = ""       # stable identifier; ARN when available
    name: str = ""
    tags: dict[str, str] = Field(default_factory=dict)

    @property
    def display(self) -> str:
        return self.name or self.resource_id or self.resource_type

    def matches(self, needle: str) -> bool:
        """Case-insensitive hint match used by the catalog resolver."""
        n = needle.lower()
        haystack = " ".join(
            [self.name, self.resource_id, self.service, self.resource_type]
            + [f"{k}={v}" for k, v in self.tags.items()]
        ).lower()
        return n in haystack


class EvidenceItem(BaseModel):
    """Ground truth produced by a tool. The report cites these by id.
    ids are deterministic in demo mode: `ev:{scenario}:{source}:{seq}`."""
    id: str
    kind: SourceKind
    resource: Optional[ResourceRef] = None
    query: str = Field(default="", description="Exact query/command executed")
    ts: Optional[TimeRange] = None
    excerpt: str = Field(description="Verbatim, capped excerpt — never paraphrased")
    score: Optional[float] = None
    raw_ref: str = Field(default="", description="Pointer to stored raw payload")
    truncated: bool = False


class Citation(BaseModel):
    """A runbook chunk grounding a recommendation (RAG)."""
    chunk_id: str
    source: str = Field(description="Runbook file name")
    section: str = Field(description="Heading breadcrumb of the chunk")
    score: float
    excerpt: str


class Finding(BaseModel):
    """An evidence-backed problem statement."""
    severity: Severity
    title: str
    detail: str
    resource: Optional[ResourceRef] = None
    cause_token: str = Field(
        default="",
        description="Stable CamelCase cause taxonomy, e.g. ConnectionPoolExhausted, "
        "TargetHealthDegraded, AccessDenied, Throttling, OOMKilled. "
        "Reuse the same token for the same class of problem.",
    )
    confidence: Confidence = "low"
    evidence_ids: list[str] = Field(default_factory=list)
    recommendation: str = ""
    citations: list[Citation] = Field(default_factory=list)


class Hypothesis(BaseModel):
    """A ranked probable cause. Confirmed ones graduate into findings;
    unverifiable ones stay hypotheses — saying so is a feature."""
    rank: int = 1
    statement: str
    status: Literal["confirmed", "refuted", "unverified"] = "unverified"
    arguments_for: list[str] = Field(default_factory=list)
    arguments_against: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)


class TimelineEvent(BaseModel):
    ts: datetime
    actor: str = Field(description="Tool/service that produced the event")
    description: str
    evidence_ids: list[str] = Field(default_factory=list)


class CoverageReport(BaseModel):
    """What the investigation actually did — the anti-hallucination receipt."""
    sources_planned: list[str] = Field(default_factory=list)
    sources_queried: list[str] = Field(default_factory=list)
    tool_calls: int = 0
    failures: list[str] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)


class InvestigationReport(BaseModel):
    investigation_id: str
    question: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    summary: str
    time_range: Optional[TimeRange] = None
    timeline: list[TimelineEvent] = Field(default_factory=list)
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
    evidence: list[EvidenceItem] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    coverage: CoverageReport = Field(default_factory=CoverageReport)
    abstained: bool = False
    abstain_reason: str = ""
    confidence: Confidence = "low"

    @property
    def has_findings(self) -> bool:
        return any(f.severity in ("critical", "high", "medium") for f in self.findings)


class ProblemStatement(BaseModel):
    """Normalized intake of the natural-language question."""
    question: str
    time_range: Optional[TimeRange] = None
    resource_hints: list[str] = Field(
        default_factory=list,
        description="Strings to resolve against the service catalog",
    )
    service: str = Field(default="", description="Catalog key if identifiable")


class ProbePlan(BaseModel):
    """What the planner decided to investigate."""
    sources: list[SourceKind]
    time_range: Optional[TimeRange] = None
    resource_targets: list[ResourceRef] = Field(default_factory=list)
    rationale: str = ""
