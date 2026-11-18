"""Graph state + Send() branch payload.

Graph shape (docs/inspiration-and-blueprint.md §4):
  START → intake → plan → gate → Send(probe_source × N) → correlate
        → hypothesize → verify → report → END

The probe fan-out follows the LangGraph map-reduce pattern (one branch per
planned source); joins are implicit via list reducers on the shared state.
"""
from __future__ import annotations

import operator
from typing import Annotated, Optional, TypedDict

from ..contracts import (
    Citation,
    EvidenceItem,
    Finding,
    Hypothesis,
    InvestigationReport,
    ProbePlan,
    ResourceRef,
    TimeRange,
    TimelineEvent,
)


def merge_list(left: Optional[list], right: Optional[list]) -> list:
    """Reducer for fan-out branches: append, tolerating first-write None."""
    return [*([] if left is None else left), *([] if right is None else right)]


class InvestigationState(TypedDict, total=False):
    """Shared state channel. Fan-out nodes only write evidence/tool_calls/timeline."""

    # intake
    investigation_id: str
    question: str
    service: str
    time_range: Optional[TimeRange]
    demo_mode: bool
    scenario: str            # fixture scenario_id when demo_mode
    demo_script: dict        # canned planner/analyst outputs for FakeLLM (offline)

    # planning
    plan: Optional[ProbePlan]

    # accumulated by probe branches (reducers)
    evidence: Annotated[list[EvidenceItem], merge_list]
    tool_calls: Annotated[list[dict], merge_list]
    timeline: Annotated[list[TimelineEvent], merge_list]
    citations: Annotated[list[Citation], merge_list]

    # analysis
    hypotheses: list[Hypothesis]
    findings: list[Finding]

    # output
    coverage: dict
    abstain_reason: str
    report: Optional[InvestigationReport]


class ProbeBranch(TypedDict, total=False):
    """Input payload of one Send('probe_source', ...) invocation."""
    investigation_id: str
    source: str                       # source adapter name
    time_range: Optional[TimeRange]
    resource_targets: list[ResourceRef]
    scenario: str                     # fixture scenario_id when demo_mode
    demo_mode: bool
