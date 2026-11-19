"""Investigation graph — compiled exactly per the plan pinned in state.py.

    START → intake → plan → gate → Send(probe_source × N) → correlate
          → hypothesize → eliminate → verify → report → END

`gate` is the typed `resolve_integrations` stage: the service catalog plus the
probe registry decide which sources are usable (unavailable ones reported with
a reason). `hypothesize` runs the diagnose stage (≥2 competing candidates with
a deterministic fallback); `eliminate` replays evidence arrival and records
which hypotheses die, so the SSE stream can show it.

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

from functools import lru_cache
from pathlib import Path

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from ..catalog import Catalog, CatalogError, load_catalog
from ..config import ConfigError, load_settings
from ..contracts import (
    CoverageReport,
    EvidenceItem,
    Finding,
    Hypothesis,
    InvestigationReport,
    ProbePlan,
    TimelineEvent,
    TimeRange,
)
from ..llm.fake import FakeLLM
from ..sources.base import ProbeContext
from ..sources.fake import load_cassette
from ..sources.probes import make_probe, registered_probes
from .diagnose import Diagnoser, DiagnoseResult, RootCauseCandidate, diagnose, to_hypotheses
from .elimination import eliminate as eliminate_candidates
from .recommend import build_plans, render
from .resolve import IntegrationResolution, resolve_integrations
from .state import InvestigationState, ProbeBranch

DEFAULT_SCENARIO = "s3_access_denied"


class CassetteDiagnoser:
    """Cassette stand-in for the analyst model.

    The script's *statuses* are not trusted: a "confirmed" hypothesis becomes a
    candidate with supporting ids, a "refuted" one with contradicting ids, and
    elimination decides the final status from evidence arrival. Unverified
    hypotheses with no resolvable ids stay unverified — that is the demo's
    honest third candidate.
    """

    def __init__(self, demo_script: dict) -> None:
        self._script = demo_script or {}

    def diagnose(
        self, question: str, evidence: list[EvidenceItem], findings: list[Finding]
    ) -> list[RootCauseCandidate]:
        candidates: list[RootCauseCandidate] = []
        for item in self._script.get("hypotheses", []):
            status = item.get("status", "unverified")
            ids = list(item.get("evidence_ids", []))
            candidates.append(
                RootCauseCandidate(
                    statement=item.get("statement", ""),
                    status="unverified",
                    supporting_ids=ids if status == "confirmed" else [],
                    contradicting_ids=ids if status == "refuted" else [],
                    required_next_evidence=list(item.get("arguments_against", []))[:2],
                    reasoning=" ".join(item.get("arguments_for", []))[:300],
                )
            )
        return candidates


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


@lru_cache(maxsize=4)
def _catalog(path: str) -> Catalog:
    """Load the service catalog once per path (relative paths resolve at repo root)."""
    candidate = Path(path)
    if not candidate.is_absolute() and not candidate.exists():
        candidate = _repo_root() / path
    return load_catalog(candidate)


# --------------------------------------------------------------------- nodes


def intake(state: InvestigationState) -> dict:
    """Normalize the NL question. Demo mode: pull the pinned cassette script."""
    question = state.get("question", "")
    scenario = state.get("scenario", "")
    demo_mode = bool(state.get("demo_mode", True))

    time_range: TimeRange | None = None
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
    """resolve_integrations: catalog + registry decide what is probeable here.

    Unavailable sources are reported (skipped) with a reason, never silently
    dropped; no usable source → abstain. A broken catalog abstains loudly
    instead of probing blind.
    """
    planned = list(state["plan"].sources) if state.get("plan") else []

    try:
        settings = load_settings()
        catalog = _catalog(settings.catalog_path)
    except (CatalogError, ConfigError) as exc:
        return {
            "integration": None,
            "coverage": {
                "sources_planned": planned,
                "skipped": [],
                "failures": [f"catalog: {exc}"],
            },
            "abstain_reason": f"service catalog unavailable: {exc}",
        }

    resolution = resolve_integrations(
        service=state.get("service", ""),
        planned_sources=planned,
        catalog=catalog,
        available_sources=registered_probes(),
    )
    coverage = {
        "sources_planned": planned,
        "skipped": [u.name for u in resolution.unavailable],
    }
    return {
        "integration": resolution.model_dump(),
        "coverage": coverage,
        "abstain_reason": resolution.abstain_reason,
    }


def route_from_gate(state: InvestigationState):
    """Conditional edge: one Send() branch per resolved source, or bail to report."""
    if state.get("abstain_reason"):
        return "report"
    raw = state.get("integration") or {}
    resolution = IntegrationResolution(**raw)
    plan: ProbePlan = state["plan"]
    return [
        Send(
            "probe_source",
            {
                "investigation_id": state.get("investigation_id", ""),
                "source": resolved.name,
                "time_range": plan.time_range,
                "resource_targets": resolved.resources or plan.resource_targets,
                "params": resolved.params,
                "scenario": state.get("scenario", ""),
                "demo_mode": bool(state.get("demo_mode", False)),
            },
        )
        for resolved in resolution.available
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
        params=branch.get("params") or {},
        scenario=branch.get("scenario", ""),
        demo_mode=bool(branch.get("demo_mode", False)),
    )
    result = make_probe(ctx).probe(ctx)

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
    """Diagnose: ≥2 competing candidates, validated; deterministic fallback if
    the analyser fails or under-delivers. Findings stay the model's narrative
    (verify decides which survive)."""
    script = state.get("demo_script") or {}
    llm = FakeLLM(script)
    findings = llm.findings()
    diagnoser: Diagnoser | None = CassetteDiagnoser(script) if script.get("hypotheses") else None
    result = diagnose(
        state.get("question", ""),
        list(state.get("evidence") or []),
        findings,
        diagnoser=diagnoser,
    )
    return {
        "candidates": result.candidates,
        "hypotheses": to_hypotheses(result),
        "findings": findings,
        "diagnose_meta": {
            "used_fallback": result.used_fallback,
            "rejected_ids": result.rejected_ids,
            "notes": result.notes,
        },
    }


def eliminate_hypotheses(state: InvestigationState) -> dict:
    """Replay evidence arrival: candidates get refuted or confirmed by code.

    The events are the SSE story ("this hypothesis just died because …");
    the hypotheses list is rebuilt so verify/report consume the final statuses.
    """
    candidates = list(state.get("candidates") or [])
    final, events = eliminate_candidates(candidates, list(state.get("evidence") or []))
    meta = dict(state.get("diagnose_meta") or {})
    result = DiagnoseResult(
        candidates=final,
        used_fallback=bool(meta.get("used_fallback", False)),
        rejected_ids=list(meta.get("rejected_ids") or []),
        notes=list(meta.get("notes") or []),
    )
    return {
        "candidates": final,
        "hypotheses": to_hypotheses(result),
        "eliminations": events,
    }


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
        # Reject-and-retry ONCE: re-run the diagnose stage. The cassette analyser
        # is deterministic, so the retry returns the same poisoned claims — the
        # demotion below is what saves us. A real model would get the validation
        # error with its second prompt.
        retry = diagnose(
            state.get("question", ""),
            list(state.get("evidence") or []),
            llm.findings(),
            diagnoser=(
                CassetteDiagnoser(state.get("demo_script") or {})
                if (state.get("demo_script") or {}).get("hypotheses")
                else None
            ),
        )
        retried_candidates, _ = eliminate_candidates(
            retry.candidates, list(state.get("evidence") or [])
        )
        hypotheses = to_hypotheses(
            DiagnoseResult(candidates=retried_candidates, used_fallback=retry.used_fallback)
        )
        findings = llm.findings()
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

    has_findings = any(f.severity in ("critical", "high", "medium") for f in findings)
    abstain_reason = state.get("abstain_reason", "")
    if not has_findings and not abstain_reason:
        abstain_reason = "insufficient evidence — no confirmed finding survived verification"
    abstained = not has_findings

    confidence = "low"
    if has_findings:
        confidence = "high" if any(h.status == "confirmed" for h in hypotheses) else "medium"

    inv = InvestigationReport(
        investigation_id=state.get("investigation_id", ""),
        question=state.get("question", ""),
        summary=FakeLLM(state.get("demo_script") or {}).summary(),
        time_range=state.get("time_range"),
        timeline=timeline,
        hypotheses=hypotheses,
        findings=findings,
        recommendations=_recommendations(findings, evidence),
        evidence=evidence,
        citations=list(citations.values()),
        coverage=coverage,
        abstained=abstained,
        abstain_reason=abstain_reason if abstained else "",
        confidence=confidence,
    )
    return {"report": inv}


def _recommendations(findings: list[Finding], evidence: list[EvidenceItem]) -> list[str]:
    """Culprit-version / rollback / verification plans, rendered to the contract."""
    plans = build_plans(findings, evidence)
    if plans:
        return [render(plan) for plan in plans]
    return [f.recommendation for f in findings if f.recommendation]


# ------------------------------------------------------------------ compile


def build_graph(
    checkpointer: BaseCheckpointSaver | None = None,
    interrupt_before: list[str] | None = None,
):
    """Compile the investigation graph.

    Offline by default: no checkpointer, no interrupts — ``scripts/smoke.py``
    and the API run fully DB-free. ``checkpointer`` wires LangGraph checkpoint
    persistence (thread_id = investigation_id, see ``store.checkpointer``);
    ``interrupt_before`` is the restart-drill seam (stop at a node boundary,
    resume from the checkpoint with the same thread_id). Node logic, reducers,
    and the offline default are untouched — this only passes compile options.
    """
    graph = StateGraph(InvestigationState)
    graph.add_node("intake", intake)
    graph.add_node("plan", plan)
    graph.add_node("gate", gate)
    graph.add_node("probe_source", probe_source)
    graph.add_node("correlate", correlate)
    graph.add_node("hypothesize", hypothesize)
    graph.add_node("eliminate", eliminate_hypotheses)
    graph.add_node("verify", verify)
    graph.add_node("report", report)

    graph.add_edge(START, "intake")
    graph.add_edge("intake", "plan")
    graph.add_edge("plan", "gate")
    graph.add_conditional_edges("gate", route_from_gate, ["probe_source", "report"])
    graph.add_edge("probe_source", "correlate")
    graph.add_edge("correlate", "hypothesize")
    graph.add_edge("hypothesize", "eliminate")
    graph.add_edge("eliminate", "verify")
    graph.add_edge("verify", "report")
    graph.add_edge("report", END)
    return graph.compile(checkpointer=checkpointer, interrupt_before=interrupt_before)


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
    question: str = "",
    scenario: str = DEFAULT_SCENARIO,
    *,
    persist: bool = False,
    checkpointer: BaseCheckpointSaver | None = None,
    thread_id: str | None = None,
    interrupt_before: list[str] | None = None,
    resume: bool = False,
) -> InvestigationReport:
    """One offline run; returns the typed report.

    Default: no database at all — no checkpointer, no persistence (smoke path).
    With a checkpointer, ``thread_id`` defaults to the investigation id minted
    by intake (``inv:{scenario}``) so checkpoints survive process restarts;
    ``resume=True`` continues an interrupted thread from its checkpoint.
    ``persist=True`` write-throughs the finished run to the Postgres ledger
    (probe results first, then the report). Interrupted legs (no report yet)
    are driven at the graph level by the restart drill, not here — this
    function returns a report or raises.
    """
    if resume and checkpointer is None:
        raise ValueError("resuming requires a checkpointer")
    if checkpointer is None and interrupt_before is None:
        app = get_graph()
    else:
        app = build_graph(checkpointer=checkpointer, interrupt_before=interrupt_before)
    state_in = None if resume else initial_state(question, scenario)
    config = None
    if checkpointer is not None:
        config = {"configurable": {"thread_id": thread_id or f"inv:{scenario or 'adhoc'}"}}
    final = app.invoke(state_in, config=config)
    report_obj = final.get("report")
    if not isinstance(report_obj, InvestigationReport):
        raise RuntimeError("graph finished without a typed InvestigationReport")
    if persist:
        _persist_run(final, report_obj)
    return report_obj


def _scope_echo(time_range: TimeRange | None) -> dict | None:
    """The window actually queried — echoed onto every persisted tool call."""
    return {"time_range": time_range.model_dump(mode="json")} if time_range else None


def _persist_run(final: dict, report: InvestigationReport) -> None:
    """Write-through the finished run to the Postgres ledger (explicit opt-in).

    Lazy import: the offline default never touches psycopg, so smoke.py stays
    DB-free even though the store ships with the package.
    """
    from ..store.ledger import persist_investigation, persist_probe_branch

    persist_probe_branch(
        report.investigation_id,
        final.get("evidence") or [],
        final.get("tool_calls") or [],
        scope=_scope_echo(final.get("time_range")),
    )
    persist_investigation(report, dict(final.get("coverage") or {}))


def run_streamed(
    question: str = "", scenario: str = DEFAULT_SCENARIO
) -> tuple[InvestigationReport, list[dict]]:
    """One offline run; returns (typed report, node-by-node event log).

    Each event is {'node': <name>, 'payload': <short summary>} — the shape the
    SSE route and the browser client consume. Parallel probe branches surface
    as separate probe_source events, one per source.
    """
    events: list[dict] = []
    report_obj: InvestigationReport | None = None
    for chunk in get_graph().stream(initial_state(question, scenario), stream_mode="updates"):
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
        if key == "eliminations" and isinstance(value, list):
            out[key] = [
                {
                    "rank": event.candidate_rank,
                    "statement": event.statement[:80],
                    "to_status": event.to_status,
                    "seq": event.seq,
                    "reason": event.reason[:120],
                }
                for event in value
            ]
        elif isinstance(value, list):
            out[key] = f"{len(value)} item(s)"
        elif isinstance(value, str):
            out[key] = value[:100]
        elif value is None:
            out[key] = None
        else:
            out[key] = type(value).__name__
    return out
