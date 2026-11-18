"""Investigation graph — compiled exactly per the plan pinned in state.py.

    START → intake → plan → gate → Send(probe_source × N) → correlate
          → hypothesize → verify → report → END

Node rules that keep the Send() fan-out safe (the Executor's predicted risk —
reducer collisions on parallel branches — is avoided BY CONSTRUCTION here):

- ``probe_source`` is the only node that runs in parallel, and it writes ONLY
  reducer channels (evidence / tool_calls / timeline / citations, all
  ``merge_list``). It never touches a LastValue channel (plan, coverage,
  hypotheses, ...), so concurrent branches cannot collide — merge_list
  tolerates any interleaving and appends deterministically.
- ``coverage`` IS a plain LastValue channel, so it is written only by
  single-branch nodes (gate → correlate → verify), each merging the previous
  dict first. Parallel probe branches must not write it — their failures are
  carried as error ToolCallRecords and folded into coverage by correlate.
- Failures are data: probes never raise into the graph.
- Evidence ids are minted only inside source adapters; verify rejects any
  claim (hypothesis or finding) whose evidence_ids do not resolve to ids
  actually present in state.
"""
from __future__ import annotations

from typing import Iterator, Optional

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from ..contracts import (
    CoverageReport,
    Finding,
    Hypothesis,
    InvestigationReport,
    ProbePlan,
    TimeRange,
    TimelineEvent,
)
from ..llm.fake import FakeLLM
from ..sources.base import ProbeContext
from ..sources.fake import load_cassette, make_source, registered_sources
from .state import InvestigationState, ProbeBranch

DEFAULT_SCENARIO = "s3_access_denied"


# --------------------------------------------------------------------- nodes


def intake(state: InvestigationState) -> dict:
    """Normalize the NL question. Demo mode: pull the pinned cassette script."""
    question = state.get("question", "")
    scenario = state.get("scenario", "")
    demo_mode = bool(state.get("demo_mode", True))

    time_range: Optional[TimeRange] = None
    demo_script: dict = {}
    service = ""
    if demo_mode and scenario:
        cassette = load_cassette(scenario)
        time_range = TimeRange(**cassette["time_range"])
        demo_script = cassette["demo_script"]
        service = demo_script.get("service", "")
        question = question or cassette.get("question", "")

    return {
        "investigation_id": f"inv:{scenario or 'adhoc'}",
        "question": question,
        "service": service,
        "time_range": time_range,
        "scenario": scenario,
        "demo_mode": demo_mode,
        "demo_script": demo_script,
    }


def plan(state: InvestigationState) -> dict:
    """FakeLLM picks which sources to probe (deterministic in demo mode)."""
    llm = FakeLLM(state.get("demo_script") or {})
    probe_plan = llm.plan(
        state.get("question", ""), state.get("service", ""), state.get("time_range")
    )
    return {"plan": probe_plan}


def gate(state: InvestigationState) -> dict:
    """Which planned sources are actually connected here. Unavailable sources
    are reported (skipped), never silently dropped; no usable source → abstain."""
    known = registered_sources()
    planned = list(state["plan"].sources) if state.get("plan") else []
    skipped = [s for s in planned if s not in known]

    abstain_reason = ""
    if not planned:
        abstain_reason = "no sources planned for this problem"
    elif len(skipped) == len(planned):
        abstain_reason = "none of the planned sources are connected: " + ", ".join(planned)

    # coverage is a plain dict channel: single-branch writers only, and each
    # one merges what previous nodes put there.
    return {
        "coverage": {"sources_planned": planned, "skipped": skipped},
        "abstain_reason": abstain_reason,
    }


def route_from_gate(state: InvestigationState):
    """Conditional edge: one Send() branch per planned source, or bail to report."""
    if state.get("abstain_reason"):
        return "report"
    plan: ProbePlan = state["plan"]
    return [
        Send(
            "probe_source",
            {
                "investigation_id": state.get("investigation_id", ""),
                "source": source,
                "time_range": plan.time_range,
                "resource_targets": plan.resource_targets,
                "scenario": state.get("scenario", ""),
                "demo_mode": bool(state.get("demo_mode", False)),
            },
        )
        for source in plan.sources
    ]


def probe_source(branch: ProbeBranch) -> dict:
    """One read-only probe against one source. Receives the Send() payload
    (a ProbeBranch) and writes ONLY reducer channels."""
    ctx = ProbeContext(
        investigation_id=branch.get("investigation_id", ""),
        source=branch["source"],
        kind=branch["source"],  # spike: adapter names are SourceKind literals
        time_range=branch.get("time_range"),
        resource_targets=branch.get("resource_targets") or [],
        scenario=branch.get("scenario", ""),
        demo_mode=bool(branch.get("demo_mode", False)),
    )
    result = make_source(ctx.source, ctx.scenario).probe(ctx)

    fallback_ts = ctx.time_range.start if ctx.time_range else None
    timeline: list[TimelineEvent] = []
    for ev in result.evidence:
        ts = ev.ts.start if ev.ts else fallback_ts
        if ts is None:
            continue
        timeline.append(
            TimelineEvent(
                ts=ts,
                actor=ctx.source,
                description=f"[{ev.id}] {ev.excerpt[:80]}",
                evidence_ids=[ev.id],
            )
        )

    return {
        "evidence": result.evidence,
        "tool_calls": [tc.model_dump() for tc in result.tool_calls],
        "timeline": timeline,
    }


def correlate(state: InvestigationState) -> dict:
    """Join point of the fan-out. Reconciles the audit trail into the honest
    coverage receipt. (Real cross-source correlation — shared resources,
    aligned timelines — is Step 2; here we join branches and compute coverage.)"""
    coverage = dict(state.get("coverage") or {})  # keep gate's skipped/sources_planned
    tool_calls = state.get("tool_calls") or []

    coverage["tool_calls"] = len(tool_calls)
    coverage["sources_queried"] = sorted(
        {tc.get("source") for tc in tool_calls if tc.get("status") == "ok"}
    )
    coverage["failures"] = sorted(
        set(coverage.get("failures", []))
        | {
            f"{tc.get('source')}: {tc.get('operation')} → {tc.get('error')}"
            for tc in tool_calls
            if tc.get("status") == "error"
        }
    )
    return {"coverage": coverage}


def hypothesize(state: InvestigationState) -> dict:
    """FakeLLM proposes competing ranked causes + candidate findings."""
    llm = FakeLLM(state.get("demo_script") or {})
    return {"hypotheses": llm.hypotheses(), "findings": llm.findings()}


def verify(state: InvestigationState) -> dict:
    """Deterministic citation-ID check — the anti-drift guard.

    Every claim's evidence_ids must resolve to EvidenceItem ids present in
    state. Violations trigger ONE reject-and-retry of the analyst output;
    anything still unresolved is demoted: hypotheses → status 'unverified'
    (invalid ids stripped), findings → downgraded to unverified hypotheses.
    A causal claim without resolvable evidence is a Hypothesis, not a Finding.
    """
    valid_ids = {e.id for e in state.get("evidence") or []}
    llm = FakeLLM(state.get("demo_script") or {})
    hypotheses = list(state.get("hypotheses") or [])
    findings = list(state.get("findings") or [])

    def invalid(claim) -> list[str]:
        return [i for i in claim.evidence_ids if i not in valid_ids]

    retried = False
    if any(invalid(c) for c in (*hypotheses, *findings)):
        # Reject-and-retry ONCE. FakeLLM is deterministic: the retry returns
        # the same poisoned claims, so demotion below is what saves us. A real
        # LLM node would receive the validation error with its second prompt.
        hypotheses, findings = llm.hypotheses(), llm.findings()
        retried = True

    kept_findings: list[Finding] = []
    demoted: list[Hypothesis] = []
    for f in findings:
        bad = invalid(f)
        if bad:
            demoted.append(
                Hypothesis(
                    statement=f"[downgraded from finding — unresolved evidence] {f.title}",
                    status="unverified",
                    evidence_ids=[i for i in f.evidence_ids if i in valid_ids],
                    arguments_against=[
                        f"cited evidence ids not present in state: {', '.join(bad)}"
                    ],
                )
            )
        else:
            kept_findings.append(f)

    verified_hypotheses: list[Hypothesis] = []
    for h in hypotheses:
        bad = invalid(h)
        if bad:
            verified_hypotheses.append(
                h.model_copy(
                    update={
                        "status": "unverified",
                        "evidence_ids": [i for i in h.evidence_ids if i in valid_ids],
                        "arguments_against": [
                            *h.arguments_against,
                            f"cited evidence ids not present in state: {', '.join(bad)}",
                        ],
                    }
                )
            )
        else:
            verified_hypotheses.append(h)

    rank = max((h.rank for h in verified_hypotheses), default=0) + 1
    for h in demoted:
        h.rank = rank
        rank += 1
    verified_hypotheses.extend(demoted)

    coverage = dict(state.get("coverage") or {})
    if retried:
        coverage["failures"] = sorted(
            set(coverage.get("failures", []))
            | {
                "verify: claims cited unknown evidence ids; analyst output "
                "rejected and retried once, unresolved claims demoted"
            }
        )

    return {
        "hypotheses": verified_hypotheses,
        "findings": kept_findings,
        "coverage": coverage,
    }


def report(state: InvestigationState) -> dict:
    """Render one typed InvestigationReport. Honors abstention + has_findings.
    Lists are re-sorted deterministically so branch-completion order can never
    change the rendered report."""
    coverage_data = dict(state.get("coverage") or {})
    coverage = CoverageReport(
        sources_planned=coverage_data.get("sources_planned", []),
        sources_queried=coverage_data.get("sources_queried", []),
        tool_calls=coverage_data.get("tool_calls", 0),
        failures=coverage_data.get("failures", []),
        skipped=coverage_data.get("skipped", []),
    )
    findings = list(state.get("findings") or [])
    hypotheses = list(state.get("hypotheses") or [])
    evidence = sorted(state.get("evidence") or [], key=lambda e: e.id)
    timeline = sorted(
        (t for t in (state.get("timeline") or []) if t.ts is not None),
        key=lambda t: t.ts,
    )

    # Citations: runbook chunks grounding recommendations. Probes may add
    # some (Step 2 RAG); findings carry theirs either way. Dedupe by identity.
    citations: dict[tuple[str, str], object] = {}
    for c in [
        *(state.get("citations") or []),
        *(c for f in findings for c in f.citations),
    ]:
        citations.setdefault((c.chunk_id, c.source), c)

    has_findings = any(
        f.severity in ("critical", "high", "medium") for f in findings
    )
    abstain_reason = state.get("abstain_reason", "")
    if not has_findings and not abstain_reason:
        abstain_reason = (
            "insufficient evidence — no confirmed finding survived verification"
        )
    abstained = not has_findings

    confidence = "low"
    if has_findings:
        confidence = (
            "high" if any(h.status == "confirmed" for h in hypotheses) else "medium"
        )

    inv = InvestigationReport(
        investigation_id=state.get("investigation_id", ""),
        question=state.get("question", ""),
        summary=FakeLLM(state.get("demo_script") or {}).summary(),
        time_range=state.get("time_range"),
        timeline=timeline,
        hypotheses=hypotheses,
        findings=findings,
        recommendations=[f.recommendation for f in findings if f.recommendation],
        evidence=evidence,
        citations=list(citations.values()),
        coverage=coverage,
        abstained=abstained,
        abstain_reason=abstain_reason if abstained else "",
        confidence=confidence,
    )
    return {"report": inv}


# ------------------------------------------------------------------ compile


def build_graph():
    graph = StateGraph(InvestigationState)
    graph.add_node("intake", intake)
    graph.add_node("plan", plan)
    graph.add_node("gate", gate)
    graph.add_node("probe_source", probe_source)
    graph.add_node("correlate", correlate)
    graph.add_node("hypothesize", hypothesize)
    graph.add_node("verify", verify)
    graph.add_node("report", report)

    graph.add_edge(START, "intake")
    graph.add_edge("intake", "plan")
    graph.add_edge("plan", "gate")
    graph.add_conditional_edges("gate", route_from_gate, ["probe_source", "report"])
    graph.add_edge("probe_source", "correlate")
    graph.add_edge("correlate", "hypothesize")
    graph.add_edge("hypothesize", "verify")
    graph.add_edge("verify", "report")
    graph.add_edge("report", END)
    return graph.compile()


_GRAPH = None


def get_graph():
    """Compile once — node functions are pure, reuse across runs is safe."""
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = build_graph()
    return _GRAPH


# ------------------------------------------------------------------- runners


def initial_state(
    question: str, scenario: str = DEFAULT_SCENARIO, demo_mode: bool = True
) -> InvestigationState:
    return {"question": question, "scenario": scenario, "demo_mode": demo_mode}


def run_investigation(
    question: str = "", scenario: str = DEFAULT_SCENARIO
) -> InvestigationReport:
    """One offline run; returns the typed report."""
    final = get_graph().invoke(initial_state(question, scenario))
    report_obj = final.get("report")
    if not isinstance(report_obj, InvestigationReport):
        raise RuntimeError("graph finished without a typed InvestigationReport")
    return report_obj


def run_streamed(
    question: str = "", scenario: str = DEFAULT_SCENARIO
) -> tuple[InvestigationReport, list[dict]]:
    """One offline run; returns (typed report, node-by-node event log).

    Each event is {'node': <name>, 'payload': <short summary>} — the shape the
    SSE route and the browser client consume. Parallel probe branches surface
    as separate probe_source events, one per source.
    """
    events: list[dict] = []
    report_obj: Optional[InvestigationReport] = None
    for chunk in get_graph().stream(
        initial_state(question, scenario), stream_mode="updates"
    ):
        for node, update in chunk.items():
            if node == "report":
                report_obj = (update or {}).get("report")
            events.append({"node": node, "payload": _short_payload(update)})
    if not isinstance(report_obj, InvestigationReport):
        raise RuntimeError("graph finished without a typed InvestigationReport")
    return report_obj, events


def _short_payload(update: dict) -> dict:
    """SSE progress payload: node name + short values — never the full report."""
    out: dict = {}
    for key, value in (update or {}).items():
        if isinstance(value, list):
            out[key] = f"{len(value)} item(s)"
        elif isinstance(value, str):
            out[key] = value[:100]
        elif value is None:
            out[key] = None
        else:
            out[key] = type(value).__name__
    return out
