"""Postgres ledger — write-through persistence and round-trip loads.

Schema decisions (adapted from docs/inspiration-and-blueprint.md §4.2):

- IDs are TEXT, not uuid: the application mints ``inv:{scenario}`` and
  ``ev:{scenario}:{source}:{seq}``, and a replayed cassette must address the
  same ledger rows.
- ``hypotheses.id`` is derived (``{investigation_id}:h{rank:02d}``) and serves
  as the idempotency key; verify may demote/re-rank between attempts, so the
  upsert UPDATES statement/status instead of duplicating rows.
- ``tool_calls.call_key`` (md5 over source/operation/params/status/error) makes
  re-persisting the same run idempotent while two genuinely different
  executions still get two audit rows — one ToolCallRecord = one row, always.
- Evidence ``excerpt`` is capped at 2000 chars in code (probes use
  ``cap_excerpt``); the ledger fails loudly if an un-capped payload reaches it.
- ``load_report()`` round-trips the full typed ``InvestigationReport``:
  columns carry what SQL queries well (ids, statuses, timestamps, excerpts),
  ``report_sections.content`` jsonb carries the typed section payloads.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Sequence

from psycopg.types.json import Json

from ..config import load_settings
from ..contracts import (
    Citation,
    CoverageReport,
    EvidenceItem,
    Finding,
    Hypothesis,
    InvestigationReport,
    ResourceRef,
    TimelineEvent,
    TimeRange,
)
from ..sources.base import EXCERPT_MAX_CHARS
from .db import connect

#: Report confidence word → numeric(3,2) for the investigations column.
CONFIDENCE_TO_NUM: dict[str, float] = {"high": 0.90, "medium": 0.60, "low": 0.30}

#: Evidence→tool-call attribution: minimum common-prefix length between the
#: evidence query and the call operation before we believe the match (guards
#: against degenerate matches such as a bare "SELECT ").
_MIN_PREFIX_MATCH = 10


# ----------------------------------------------------------------- internals


def _as_evidence(item: EvidenceItem | dict) -> EvidenceItem:
    return item if isinstance(item, EvidenceItem) else EvidenceItem(**item)


def _call_key(tc: dict) -> str:
    """Natural idempotency key of one tool call record."""
    payload = "\x1f".join(
        [
            str(tc.get("source", "")),
            str(tc.get("operation", "")),
            json.dumps(tc.get("params") or {}, sort_keys=True, default=str),
            str(tc.get("status", "ok")),
            str(tc.get("error", "")),
        ]
    )
    return hashlib.md5(payload.encode("utf-8")).hexdigest()


def _normalized_operation(operation: str) -> str:
    """Strip the read-only SQL prefix so 'query SELECT ...' matches 'SELECT ...'."""
    op = (operation or "").strip()
    return op[6:] if op.lower().startswith("query ") else op


def _common_prefix_len(a: str, b: str) -> int:
    n = 0
    for x, y in zip(a, b, strict=False):
        if x != y:
            break
        n += 1
    return n


def _pair_evidence_to_calls(
    evidence: Sequence[EvidenceItem], tool_calls: Sequence[dict]
) -> dict[int, list[EvidenceItem]]:
    """Best-effort attribution of each evidence item to its minting tool call.

    The probe envelope (ProbeResult) flattens the call→evidence link, so the
    ledger reconstructs it: within the same source, the call whose normalized
    operation shares the longest common prefix with the evidence ``query`` wins
    (probe layers record the executed query either as the bare operation or a
    fuller rendering of it). Below the confidence floor the item falls back to
    the first non-error call of its source; sources that never answered ok
    leave the item unattributed (tool_call_seq NULL) rather than guessed.
    """
    attribution: dict[int, list[EvidenceItem]] = defaultdict(list)
    ok_calls: dict[str, list[int]] = defaultdict(list)
    for idx, tc in enumerate(tool_calls):
        # EvidenceItem.kind is the minting source's registered name (SourceKind);
        # ToolCallRecord dicts carry the same name in `source`.
        if tc.get("status", "ok") == "ok":
            ok_calls[str(tc.get("source", ""))].append(idx)

    for ev in evidence:
        candidates = ok_calls.get(str(ev.kind), [])
        if not candidates:
            continue
        best_idx: int | None = None
        best_len = 0
        for idx in candidates:
            prefix = _common_prefix_len(
                ev.query or "", _normalized_operation(str(tool_calls[idx].get("operation", "")))
            )
            if prefix > best_len:
                best_idx, best_len = idx, prefix
        if best_idx is None or best_len < _MIN_PREFIX_MATCH:
            best_idx = candidates[0]
        attribution[best_idx].append(ev)
    return attribution


def _resource_ref_text(ev: EvidenceItem) -> str | None:
    if ev.resource is None:
        return None
    return ev.resource.resource_id or ev.resource.name or ev.resource.resource_type or None


def _dominant_service(report: InvestigationReport) -> str | None:
    """The service the evidence actually points at — derived, never invented."""
    counts = Counter(
        ev.resource.service for ev in report.evidence if ev.resource and ev.resource.service
    )
    return counts.most_common(1)[0][0] if counts else None


def _hypothesis_id(investigation_id: str, rank: int) -> str:
    return f"{investigation_id}:h{rank:02d}"


def _evidence_row(investigation_id: str, ev: EvidenceItem, seq: int | None) -> dict:
    if len(ev.excerpt) > EXCERPT_MAX_CHARS:
        raise ValueError(
            f"evidence {ev.id} excerpt is {len(ev.excerpt)} chars — exceeds the "
            f"{EXCERPT_MAX_CHARS}-char cap enforced in code; the probe layer must "
            "apply cap_excerpt before persistence"
        )
    detail = {
        "kind": ev.kind,
        "score": ev.score,
        "raw_ref": ev.raw_ref,
        "truncated": ev.truncated,
        "resource": ev.resource.model_dump(mode="json") if ev.resource else None,
        "ts_label": ev.ts.label if ev.ts else None,
    }
    return {
        "id": ev.id,
        "investigation_id": investigation_id,
        "source_type": ev.kind,
        "resource_ref": _resource_ref_text(ev),
        "ts_start": ev.ts.start if ev.ts else None,
        "ts_end": ev.ts.end if ev.ts else None,
        "excerpt": ev.excerpt,
        "detail": Json(detail),
        "query_executed": ev.query,
        "query_hash": hashlib.md5((ev.query or "").encode("utf-8")).hexdigest()
        if ev.query
        else None,
        "tool_call_seq": seq,
    }


_EVIDENCE_INSERT = """
INSERT INTO evidence (id, investigation_id, source_type, resource_ref, ts_start, ts_end,
                      excerpt, detail, query_executed, query_hash, tool_call_seq)
VALUES (%(id)s, %(investigation_id)s, %(source_type)s, %(resource_ref)s, %(ts_start)s,
        %(ts_end)s, %(excerpt)s, %(detail)s, %(query_executed)s, %(query_hash)s,
        %(tool_call_seq)s)
ON CONFLICT (id) DO NOTHING
"""

_TOOL_CALL_INSERT = """
INSERT INTO tool_calls (investigation_id, source, operation, params, scope, status,
                        duration_ms, rows_returned, truncated, error, call_key)
VALUES (%(investigation_id)s, %(source)s, %(operation)s, %(params)s, %(scope)s,
        %(status)s, %(duration_ms)s, %(rows_returned)s, %(truncated)s, %(error)s,
        %(call_key)s)
ON CONFLICT (investigation_id, call_key) DO NOTHING
"""

_INVESTIGATION_UPSERT = """
INSERT INTO investigations (id, question, service_id, env, status, summary, confidence,
                             started_at, finished_at)
VALUES (%(id)s, %(question)s, %(service_id)s, %(env)s, %(status)s, %(summary)s,
        %(confidence)s, %(started_at)s, %(finished_at)s)
ON CONFLICT (id) DO UPDATE SET
  question = EXCLUDED.question,
  service_id = EXCLUDED.service_id,
  env = EXCLUDED.env,
  status = EXCLUDED.status,
  summary = EXCLUDED.summary,
  confidence = EXCLUDED.confidence,
  finished_at = EXCLUDED.finished_at
"""

_HYPOTHESIS_UPSERT = """
INSERT INTO hypotheses (id, investigation_id, rank, title, statement, rationale,
                        confidence, status, arguments_for, arguments_against, evidence_ids)
VALUES (%(id)s, %(investigation_id)s, %(rank)s, %(title)s, %(statement)s, %(rationale)s,
        %(confidence)s, %(status)s, %(arguments_for)s, %(arguments_against)s,
        %(evidence_ids)s)
ON CONFLICT (id) DO UPDATE SET
  rank = EXCLUDED.rank,
  title = EXCLUDED.title,
  statement = EXCLUDED.statement,
  status = EXCLUDED.status,
  arguments_for = EXCLUDED.arguments_for,
  arguments_against = EXCLUDED.arguments_against,
  evidence_ids = EXCLUDED.evidence_ids
"""

_SECTION_UPSERT = """
INSERT INTO report_sections (report_id, section_id, title, content)
VALUES (%(report_id)s, %(section_id)s, %(title)s, %(content)s)
ON CONFLICT (report_id, section_id) DO UPDATE SET
  title = EXCLUDED.title,
  content = EXCLUDED.content
"""


def _ensure_investigation(conn, investigation_id: str, env: str) -> None:
    """Write-through stub so per-branch persists can land before the report."""
    conn.execute(
        "INSERT INTO investigations (id, question, env, status) "
        "VALUES (%s, '', %s, 'running') ON CONFLICT (id) DO NOTHING",
        (investigation_id, env),
    )


def _ensure_service(conn, service_id: str | None, env: str) -> None:
    """FK safety: upsert a minimal services row; the catalog loader fills it later."""
    if not service_id:
        return
    conn.execute(
        "INSERT INTO services (id, env, spec) VALUES (%s, %s, %s) ON CONFLICT (id) DO NOTHING",
        (service_id, env, Json({})),
    )


def _sections(report: InvestigationReport, coverage: dict) -> list[dict]:
    """The typed report as addressable jsonb sections (round-trip payload)."""
    cov = CoverageReport(**coverage) if coverage else report.coverage
    rows = [
        {
            "section_id": "time_range",
            "title": "Time range",
            "content": Json(
                report.time_range.model_dump(mode="json") if report.time_range else None
            ),
        },
        {
            "section_id": "timeline",
            "title": "Timeline",
            "content": Json([t.model_dump(mode="json") for t in report.timeline]),
        },
        {
            "section_id": "findings",
            "title": "Findings",
            "content": Json([f.model_dump(mode="json") for f in report.findings]),
        },
        {
            "section_id": "recommendations",
            "title": "Recommendations",
            "content": Json(list(report.recommendations)),
        },
        {
            "section_id": "citations",
            "title": "Runbook citations",
            "content": Json([c.model_dump(mode="json") for c in report.citations]),
        },
        {
            "section_id": "coverage",
            "title": "Coverage",
            "content": Json(cov.model_dump(mode="json")),
        },
        {
            "section_id": "abstention",
            "title": "Abstention",
            "content": Json(
                {"abstained": report.abstained, "abstain_reason": report.abstain_reason}
            ),
        },
        {
            "section_id": "confidence",
            "title": "Confidence",
            "content": Json({"confidence": report.confidence}),
        },
        {
            "section_id": "meta",
            "title": "Report metadata",
            "content": Json({"created_at": report.created_at.isoformat()}),
        },
    ]
    return rows


# ------------------------------------------------------------ write-through


def persist_probe_branch(
    investigation_id: str,
    evidence: Sequence[EvidenceItem | dict],
    tool_call_dicts: Sequence[dict],
    scope: dict | None = None,
) -> None:
    """Persist one probe branch (or the merged equivalent) — idempotent.

    Writes: investigations stub (status 'running', if absent) + one row per
    tool call (deduped by call_key) + one row per evidence item (ON CONFLICT
    DO NOTHING — a re-persist never duplicates or overwrites evidence).
    Safe to call per-branch during a fan-out or once with the merged lists.
    """
    env = load_settings().environment
    items = [_as_evidence(e) for e in evidence]
    calls = [dict(tc) for tc in tool_call_dicts]
    attribution = _pair_evidence_to_calls(items, calls)

    with connect() as conn, conn.transaction():
        _ensure_investigation(conn, investigation_id, env)
        for idx, tc in enumerate(calls):
            owned = attribution.get(idx, [])
            conn.execute(
                _TOOL_CALL_INSERT,
                {
                    "investigation_id": investigation_id,
                    "source": tc.get("source"),
                    "operation": tc.get("operation"),
                    "params": Json(tc.get("params") or {}),
                    "scope": Json(scope) if scope else None,
                    "status": tc.get("status", "ok"),
                    "duration_ms": tc.get("duration_ms"),
                    "rows_returned": len(owned),
                    "truncated": any(ev.truncated for ev in owned),
                    "error": tc.get("error") or None,
                    "call_key": _call_key(tc),
                },
            )
        owner_of = {id(ev): idx for idx, owned in attribution.items() for ev in owned}
        for ev in items:
            row = _evidence_row(investigation_id, ev, owner_of.get(id(ev)))
            conn.execute(_EVIDENCE_INSERT, row)


def persist_investigation(report: InvestigationReport, coverage: dict) -> None:
    """Persist the finished investigation: row, hypotheses, report sections.

    Idempotent: investigations/hypotheses upsert by id, the reports row is
    created only if absent, sections upsert by (report_id, section_id) —
    persisting the same report twice leaves row counts unchanged.
    """
    env = load_settings().environment
    service_id = _dominant_service(report)

    with connect() as conn, conn.transaction():
        _ensure_service(conn, service_id, env)
        conn.execute(
            _INVESTIGATION_UPSERT,
            {
                "id": report.investigation_id,
                "question": report.question,
                "service_id": service_id,
                "env": env,
                "status": "abstained" if report.abstained else "done",
                "summary": report.summary,
                "confidence": CONFIDENCE_TO_NUM[report.confidence],
                "started_at": report.created_at,
                "finished_at": report.created_at,
            },
        )
        for h in report.hypotheses:
            conn.execute(
                _HYPOTHESIS_UPSERT,
                {
                    "id": _hypothesis_id(report.investigation_id, h.rank),
                    "investigation_id": report.investigation_id,
                    "rank": h.rank,
                    # Browsing aid; the contract carries statement only.
                    "title": h.statement[:80],
                    "statement": h.statement,
                    "rationale": None,
                    "confidence": None,
                    "status": h.status,
                    "arguments_for": Json(list(h.arguments_for)),
                    "arguments_against": Json(list(h.arguments_against)),
                    "evidence_ids": list(h.evidence_ids),
                },
            )

        row = conn.execute(
            "SELECT id FROM reports WHERE investigation_id = %s ORDER BY id LIMIT 1",
            (report.investigation_id,),
        ).fetchone()
        if row is None:
            report_id = conn.execute(
                "INSERT INTO reports (investigation_id) VALUES (%s) RETURNING id",
                (report.investigation_id,),
            ).fetchone()[0]
        else:
            report_id = row[0]

        for section in _sections(report, coverage):
            conn.execute(_SECTION_UPSERT, {**section, "report_id": report_id})


# ---------------------------------------------------------------- round-trip


def load_report(investigation_id: str) -> InvestigationReport:
    """Rebuild the typed InvestigationReport from the ledger.

    Raises KeyError when the investigation (or a report section) is absent —
    the replay checker must never fabricate a partial report.
    """
    with connect() as conn:
        inv = conn.execute(
            "SELECT id, question, summary FROM investigations WHERE id = %s",
            (investigation_id,),
        ).fetchone()
        if inv is None:
            raise KeyError(f"no investigation {investigation_id!r} in the ledger")

        evidence_rows = conn.execute(
            "SELECT id, source_type, resource_ref, ts_start, ts_end, excerpt, detail,"
            " query_executed, query_hash, tool_call_seq"
            " FROM evidence WHERE investigation_id = %s ORDER BY id",
            (investigation_id,),
        ).fetchall()
        hypothesis_rows = conn.execute(
            "SELECT rank, statement, status, arguments_for, arguments_against, evidence_ids"
            " FROM hypotheses WHERE investigation_id = %s ORDER BY rank",
            (investigation_id,),
        ).fetchall()

        report_row = conn.execute(
            "SELECT id FROM reports WHERE investigation_id = %s ORDER BY id DESC LIMIT 1",
            (investigation_id,),
        ).fetchone()
        if report_row is None:
            raise KeyError(f"investigation {investigation_id!r} has no persisted report")
        sections = {
            row[0]: row[1]
            for row in conn.execute(
                "SELECT section_id, content FROM report_sections WHERE report_id = %s",
                (report_row[0],),
            ).fetchall()
        }

        def section(sid: str) -> dict:
            if sid not in sections or sections[sid] is None:
                raise KeyError(
                    f"investigation {investigation_id!r}: report section {sid!r} "
                    "missing — ledger write incomplete"
                )
            return sections[sid]

        evidence = [_evidence_from_row(r) for r in evidence_rows]
        hypotheses = [
            Hypothesis(
                rank=r[0],
                statement=r[1],
                status=r[2],
                arguments_for=list(r[3] or []),
                arguments_against=list(r[4] or []),
                evidence_ids=list(r[5] or []),
            )
            for r in hypothesis_rows
        ]
        meta = section("meta")
        abstention = section("abstention")
        # time_range is the one optional section (jsonb NULL when unwindowed).
        time_range_payload = sections.get("time_range")

        return InvestigationReport(
            investigation_id=inv[0],
            question=inv[1],
            created_at=meta["created_at"],
            summary=inv[2] or "",
            time_range=TimeRange(**time_range_payload) if time_range_payload else None,
            timeline=[TimelineEvent(**t) for t in section("timeline")],
            hypotheses=hypotheses,
            findings=[Finding(**f) for f in section("findings")],
            recommendations=list(section("recommendations") or []),
            evidence=evidence,
            citations=[Citation(**c) for c in section("citations")],
            coverage=CoverageReport(**section("coverage")),
            abstained=bool(abstention["abstained"]),
            abstain_reason=abstention.get("abstain_reason", ""),
            confidence=section("confidence")["confidence"],
        )


def _evidence_from_row(row) -> EvidenceItem:
    (_id, source_type, _ref, ts_start, ts_end, excerpt, detail, query, _hash, _seq) = row
    detail = detail or {}
    return EvidenceItem(
        id=_id,
        kind=detail.get("kind", source_type),
        resource=ResourceRef(**detail["resource"]) if detail.get("resource") else None,
        query=query or "",
        ts=TimeRange(start=ts_start, end=ts_end, label=detail.get("ts_label") or "")
        if ts_start is not None and ts_end is not None
        else None,
        excerpt=excerpt,
        score=detail.get("score"),
        raw_ref=detail.get("raw_ref") or "",
        truncated=bool(detail.get("truncated", False)),
    )


# ----------------------------------------------------------------- drill API


def ledger_counts(investigation_id: str) -> dict:
    """Ledger row counts + hypothesis statuses — the restart-drill comparator."""
    with connect() as conn:
        present = (
            conn.execute(
                "SELECT 1 FROM investigations WHERE id = %s", (investigation_id,)
            ).fetchone()
            is not None
        )
        counts = {"present": present, "investigation_id": investigation_id}
        if not present:
            return {
                **counts,
                "evidence": 0,
                "tool_calls": 0,
                "hypotheses": 0,
                "hypothesis_statuses": [],
                "reports": 0,
                "report_sections": 0,
            }
        for key, table in (
            ("evidence", "evidence"),
            ("tool_calls", "tool_calls"),
            ("hypotheses", "hypotheses"),
            ("reports", "reports"),
        ):
            # table is from the fixed tuple above, never user input
            counts[key] = conn.execute(
                f"SELECT count(*) FROM {table} WHERE investigation_id = %s",
                (investigation_id,),
            ).fetchone()[0]
        counts["report_sections"] = conn.execute(
            "SELECT count(*) FROM report_sections WHERE report_id IN"
            " (SELECT id FROM reports WHERE investigation_id = %s)",
            (investigation_id,),
        ).fetchone()[0]
        counts["hypothesis_statuses"] = [
            (r[0], r[1])
            for r in conn.execute(
                "SELECT rank, status FROM hypotheses WHERE investigation_id = %s ORDER BY rank",
                (investigation_id,),
            ).fetchall()
        ]
        return counts


def delete_investigation(investigation_id: str) -> bool:
    """Remove one investigation (cascade). Drill/test reset helper."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM investigations WHERE id = %s", (investigation_id,))
        return cur.rowcount > 0
