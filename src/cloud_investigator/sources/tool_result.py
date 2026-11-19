"""StructuredToolResult — the uniform tool envelope (HolmesGPT-style).

Design rules pinned by the plan (Phase 1, tool layer) and AGENTS.md §7:

- ``NO_DATA`` is not ``ERROR``: a successful read that found nothing is status
  ``no_data`` — data, not a failure, and never a silent skip.
- Upstream failures are recorded VERBATIM (``repr(exc)``): the envelope never
  reformulates, summarizes, or hides an upstream error.
- Read-only is fail-closed: ``assert_read_only`` (base.py LAW) runs FIRST inside
  the one auditable ``call()`` seam. A rejected operation becomes a ``denied``
  envelope (→ ``ToolCallRecord(status="skipped")`` with the reason) and never
  raises into the graph.
- Every result carries a scope echo (what was ACTUALLY queried, after clipping
  and capping), default limits, and explicit truncation markers.
- Items are flat rows; every payload value is capped via ``cap_excerpt`` before
  it can enter state or an LLM context.
- Evidence ids are minted only here (``make_evidence_id``), never by LLM nodes.

The envelope is source-agnostic: CloudWatch, SQL and future probes all return
``StructuredToolResult`` and fold into ``ProbeResult`` via
``ProbeResultBuilder``, so the graph consumes one shape.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, NamedTuple

from pydantic import BaseModel, Field

from ..contracts import EvidenceItem, TimeRange
from .base import (
    EXCERPT_MAX_CHARS,
    MAX_EVIDENCE_PER_PROBE,
    ProbeContext,
    ProbeResult,
    ToolCallRecord,
    assert_read_only,
    cap_excerpt,
    make_evidence_id,
)

ToolStatus = Literal["ok", "error", "denied", "skipped_budget", "no_data"]

#: Ledger mapping — ToolCallRecord (base.py LAW) is coarser than the envelope:
#: a pre-execution rejection (denied / skipped_budget) becomes status="skipped"
#: with the reason in `error`; a successful empty read stays status="ok".
_RECORD_STATUS: dict[str, str] = {
    "ok": "ok",
    "no_data": "ok",
    "error": "error",
    "denied": "skipped",
    "skipped_budget": "skipped",
}


class Item(BaseModel):
    """One flat observation row: when, where, what — and the verbatim value."""

    ts: str = ""  # ISO-8601 UTC; "" when the source has no timestamp
    resource: str = ""  # log group, database, table, ...
    key: str = ""  # row/event discriminator (event id, row number, ...)
    value: str = ""  # verbatim payload — always capped before storage


class StructuredToolResult(BaseModel):
    """HolmesGPT-style tool envelope: outcome + scope echo + bounded items.

    ``params`` echoes the request; ``scope`` echoes what was ACTUALLY queried
    after guards, clipping and capping. ``error`` carries the verbatim upstream
    error (or the rejection reason for denied / skipped_budget).
    """

    status: ToolStatus
    operation: str = ""
    params: dict[str, Any] = Field(default_factory=dict)
    scope: dict[str, Any] = Field(default_factory=dict)
    items: list[Item] = Field(default_factory=list)
    truncated: bool = False
    next_page: str | None = None
    duration_ms: int = 0
    error: str = ""
    rows_returned: int = 0

    def to_tool_call_record(self, source: str) -> ToolCallRecord:
        """Flatten onto the coarse ledger row shape the graph persists."""
        return ToolCallRecord(
            source=source,
            operation=self.operation,
            params=self.params,
            status=_RECORD_STATUS[self.status],
            duration_ms=self.duration_ms,
            error=self.error,
        )


# ------------------------------------------------------------------ factories
#
# The pinned API wants BOTH a field `error` (verbatim upstream error) and a
# constructor `.error(op, repr(exc))`. pydantic v2's metaclass silently drops
# class methods that share a field name, and a mixin base makes pydantic warn
# about shadowing on every build. The honest resolution is to attach the
# factory classmethods AFTER the class exists: class access finds the
# constructor, instance access finds the field value (stored per-instance).


def _ok_factory(
    cls: type[StructuredToolResult],
    operation: str,
    *,
    params: dict[str, Any] | None = None,
    scope: dict[str, Any] | None = None,
    items: list[Item] | None = None,
    truncated: bool = False,
    next_page: str | None = None,
    duration_ms: int = 0,
) -> StructuredToolResult:
    rows = list(items or [])
    return cls(
        status="ok",
        operation=operation,
        params=params or {},
        scope=scope or {},
        items=rows,
        truncated=truncated,
        next_page=next_page,
        duration_ms=duration_ms,
        rows_returned=len(rows),
    )


def _error_factory(
    cls: type[StructuredToolResult],
    operation: str,
    error: str,
    *,
    params: dict[str, Any] | None = None,
    scope: dict[str, Any] | None = None,
    duration_ms: int = 0,
) -> StructuredToolResult:
    return cls(
        status="error",
        operation=operation,
        params=params or {},
        scope=scope or {},
        duration_ms=duration_ms,
        error=error,
    )


def _denied_factory(
    cls: type[StructuredToolResult],
    operation: str,
    reason: str,
    *,
    params: dict[str, Any] | None = None,
    scope: dict[str, Any] | None = None,
    duration_ms: int = 0,
) -> StructuredToolResult:
    """Pre-execution policy rejection — nothing ran, nothing was asked."""
    return cls(
        status="denied",
        operation=operation,
        params=params or {},
        scope=scope or {},
        duration_ms=duration_ms,
        error=reason,
    )


def _skipped_budget_factory(
    cls: type[StructuredToolResult],
    operation: str,
    reason: str,
    *,
    params: dict[str, Any] | None = None,
    scope: dict[str, Any] | None = None,
    duration_ms: int = 0,
) -> StructuredToolResult:
    return cls(
        status="skipped_budget",
        operation=operation,
        params=params or {},
        scope=scope or {},
        duration_ms=duration_ms,
        error=reason,
    )


def _no_data_factory(
    cls: type[StructuredToolResult],
    operation: str,
    *,
    params: dict[str, Any] | None = None,
    scope: dict[str, Any] | None = None,
    duration_ms: int = 0,
) -> StructuredToolResult:
    """A successful read that found nothing — NOT an error (contract §7)."""
    return cls(
        status="no_data",
        operation=operation,
        params=params or {},
        scope=scope or {},
        duration_ms=duration_ms,
    )


StructuredToolResult.ok = classmethod(_ok_factory)  # type: ignore[attr-defined]
StructuredToolResult.error = classmethod(_error_factory)  # type: ignore[attr-defined]
StructuredToolResult.denied = classmethod(_denied_factory)  # type: ignore[attr-defined]
StructuredToolResult.skipped_budget = classmethod(_skipped_budget_factory)  # type: ignore[attr-defined]
StructuredToolResult.no_data = classmethod(_no_data_factory)  # type: ignore[attr-defined]


class RawResult(NamedTuple):
    """What an executor hands back to the seam, before any capping."""

    rows: list[Item]
    next_page: str | None = None
    truncated: bool = False  # executor knows a full page may hide more data


#: Executor: runs the (already guarded) operation, returns raw rows.
Executor = Callable[[str, dict[str, Any]], RawResult]


def call(
    source: str,
    op: str,
    params: dict[str, Any],
    *,
    ctx: ProbeContext,
    execute: Executor,
    scope: dict[str, Any] | None = None,
    max_rows: int = MAX_EVIDENCE_PER_PROBE,
    budget_left: int | None = None,
) -> StructuredToolResult:
    """The ONE auditable execution seam every probe funnels through.

    Order is policy, not preference:

    1. ``assert_read_only(op)`` FIRST (fail-closed, base.py LAW) — a rejected
       operation becomes a ``denied`` envelope, never an exception.
    2. Budget: when ``budget_left`` is exhausted the call is skipped BEFORE the
       client is touched (``skipped_budget``); a partial budget caps the rows
       this call may return.
    3. Execution: any exception becomes an ``error`` envelope with the VERBATIM
       ``repr(exc)``.
    4. Caps: rows are cut to the effective cap, every payload value runs through
       ``cap_excerpt``, and truncation is marked explicitly.

    The seam never raises for policy or upstream reasons. A ``source`` that
    disagrees with ``ctx.source`` is a wiring bug and fails fast instead.
    """
    if source != ctx.source:
        raise ValueError(f"wiring bug: call(source={source!r}) but ctx.source={ctx.source!r}")
    echoed = dict(scope or {})
    started = time.perf_counter()

    def elapsed() -> int:
        return int((time.perf_counter() - started) * 1000)

    # 1) fail-closed read-only guard — a denied op is a record, never a raise.
    try:
        assert_read_only(op)
    except (PermissionError, ValueError) as exc:
        return StructuredToolResult.denied(
            op, str(exc), params=params, scope=echoed, duration_ms=elapsed()
        )

    # 2) budget: skip before touching the client when the row budget is spent.
    if budget_left is not None and budget_left <= 0:
        return StructuredToolResult.skipped_budget(
            op,
            "row budget exhausted — call not executed",
            params=params,
            scope=echoed,
            duration_ms=elapsed(),
        )
    cap = max_rows if budget_left is None else min(max_rows, budget_left)

    # 3) execution — upstream failures become verbatim error envelopes.
    try:
        raw = execute(op, params)
    except Exception as exc:  # recording beats reformulating; never re-raise
        return StructuredToolResult.error(
            op, repr(exc), params=params, scope=echoed, duration_ms=elapsed()
        )

    # 4) caps: row count + every payload value, with explicit truncation markers.
    rows = list(raw.rows)[:cap]
    truncated = raw.truncated or len(raw.rows) > cap
    items: list[Item] = []
    for row in rows:
        value, capped = cap_excerpt(row.value, EXCERPT_MAX_CHARS)
        truncated = truncated or capped
        items.append(row.model_copy(update={"value": value}))

    if not items:
        return StructuredToolResult.no_data(op, params=params, scope=echoed, duration_ms=elapsed())
    return StructuredToolResult.ok(
        op,
        params=params,
        scope=echoed,
        items=items,
        truncated=truncated,
        next_page=raw.next_page,
        duration_ms=elapsed(),
    )


# ------------------------------------------------------------------- folding


def _point_range(ts: str) -> TimeRange | None:
    """A point-in-time TimeRange for one item; None when ts is absent/invalid."""
    if not ts:
        return None
    try:
        stamp = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return TimeRange(start=stamp, end=stamp)


class ProbeResultBuilder:
    """Fold envelopes into the uniform ``ProbeResult`` the graph consumes.

    Evidence ids are minted here — and only here, inside the tool layer — via
    ``make_evidence_id``; the seq counter is scoped to (scenario, source).
    Error envelopes append to ``failures`` verbatim; denied / skipped_budget
    envelopes surface through ``skipped_reason`` when nothing else was yielded.
    """

    def __init__(self, ctx: ProbeContext, *, raw_ref_prefix: str) -> None:
        self._ctx = ctx
        self._raw_ref_prefix = raw_ref_prefix
        self._seq = 0
        self._evidence: list[EvidenceItem] = []
        self._tool_calls: list[ToolCallRecord] = []
        self._failures: list[str] = []
        self._skips: list[str] = []

    @property
    def evidence_count(self) -> int:
        return len(self._evidence)

    def add(self, envelope: StructuredToolResult, *, query: str = "") -> None:
        self._tool_calls.append(envelope.to_tool_call_record(self._ctx.source))
        if envelope.status == "error":
            self._failures.append(f"{self._ctx.source}: {envelope.operation} → {envelope.error}")
        elif envelope.status in ("denied", "skipped_budget"):
            self._skips.append(f"{self._ctx.source}: {envelope.operation} → {envelope.error}")
        if envelope.status != "ok":
            return
        last = len(envelope.items) - 1
        for position, item in enumerate(envelope.items):
            self._seq += 1
            excerpt, capped = cap_excerpt(item.value)
            self._evidence.append(
                EvidenceItem(
                    id=make_evidence_id(self._ctx.scenario, self._ctx.source, self._seq),
                    kind=self._ctx.kind,
                    resource=(
                        self._ctx.resource_targets[0] if self._ctx.resource_targets else None
                    ),
                    query=query or str(envelope.scope.get("sql") or envelope.operation),
                    ts=_point_range(item.ts),
                    excerpt=excerpt,
                    raw_ref=f"{self._raw_ref_prefix}#{self._seq}",
                    # per-item excerpt cap, plus a cut marker on the last row
                    # when the result set itself was truncated.
                    truncated=capped or (envelope.truncated and position == last),
                )
            )

    def result(self) -> ProbeResult:
        skipped_reason = "; ".join(self._skips) if self._skips and not self._evidence else ""
        return ProbeResult(
            evidence=self._evidence,
            tool_calls=self._tool_calls,
            failures=self._failures,
            skipped_reason=skipped_reason,
        )


# ------------------------------------------------------------------- helpers


def iso_utc(value: Any) -> str:
    """Normalize a datetime/str into an ISO-8601 UTC string ("" when absent)."""
    if isinstance(value, datetime):
        stamped = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return stamped.isoformat().replace("+00:00", "Z")
    if isinstance(value, str):
        return value
    return ""


def raw_ref_prefix(ctx: ProbeContext) -> str:
    """Stable raw-payload pointer prefix: cassette demo vs live run."""
    if ctx.demo_mode:
        return f"cassette:{ctx.scenario}:{ctx.source}"
    return f"live:{ctx.investigation_id or 'adhoc'}:{ctx.source}"


def _probes_dir() -> Path:
    """Locate fixtures/probes/ — env override, then CWD, then repo layout
    (same resolution order as the top-level cassette loader in fake.py)."""
    override = os.environ.get("CLOUD_INVESTIGATOR_FIXTURES")
    if override:
        return Path(override) / "probes"
    local = Path.cwd() / "fixtures" / "probes"
    if local.is_dir():
        return local
    return Path(__file__).resolve().parents[3] / "fixtures" / "probes"


@lru_cache(maxsize=8)
def load_probe_cassette(kind: str, scenario: str) -> dict:
    """Load ``fixtures/probes/<kind>_<scenario>.json``.

    Cached and shared: every consumer must treat the returned dict as
    read-only (same contract as ``fake.load_cassette``).
    """
    path = _probes_dir() / f"{kind}_{scenario}.json"
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)
