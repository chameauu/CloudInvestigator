"""Source adapter protocol — the pinned interface every data source implements.

Design rules (from the inspiration set — HolmesGPT engineering notes, incident-suite,
the council's 'read-only is architecture'):
- One auditable chokepoint per provider; operations must pass `assert_read_only`.
- Every probe is scoped: mandatory time range, row/line caps, truncation flags.
- Every probe returns the same envelope: evidence + what was executed + failures.
- Unbounded data is forbidden: `cap_excerpt()` is applied to everything stored.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from ..contracts import EvidenceItem, ResourceRef, SourceKind, TimeRange

# ---------------------------------------------------------------- guard rails

EXCERPT_MAX_CHARS = 2000
MAX_EVIDENCE_PER_PROBE = 40

#: Operation allowlist — fail closed. Anything not matching is rejected
#: *before* a client is created, so a prompt-injected "delete" never runs.
READ_PREFIXES = ("describe", "list", "get", "query", "search", "filter")
WRITE_VERBS = (
    "create",
    "delete",
    "update",
    "put",
    "post",
    "patch",
    "terminate",
    "stop",
    "start",
    "reboot",
    "modify",
    "attach",
    "detach",
    "tag",
    "run",
    "execute",
    "insert",
    "drop",
    "alter",
    "grant",
    "revoke",
    "truncate",
)


def assert_read_only(operation: str) -> None:
    """Raise if an operation is not a recognized read. Fail-closed allowlist."""
    op = (operation or "").strip().lower()
    if not op:
        raise ValueError("empty operation")
    if any(verb in op for verb in WRITE_VERBS):
        raise PermissionError(f"write-like operation rejected by read-only guard: {operation!r}")
    if not op.startswith(READ_PREFIXES):
        raise PermissionError(f"operation not in read allowlist: {operation!r}")


def cap_excerpt(text: str, limit: int = EXCERPT_MAX_CHARS) -> tuple[str, bool]:
    """Cap any payload before it can enter state/LLM context."""
    if text is None:
        return "", False
    if len(text) <= limit:
        return text, False
    return text[:limit] + f"\n…[truncated {len(text) - limit} chars]", True


def make_evidence_id(scenario: str, source: str, seq: int) -> str:
    """Deterministic evidence ids so demo fixtures and reports stay in sync."""
    return f"ev:{scenario or 'live'}:{source}:{seq}"


# ---------------------------------------------------------------- envelopes


class ToolCallRecord(BaseModel):
    """Audit trail of one executed call — feeds coverage + timeline."""

    source: str
    operation: str
    params: dict = Field(default_factory=dict)
    status: Literal["ok", "error", "skipped"] = "ok"
    duration_ms: int = 0
    error: str = ""


class ProbeContext(BaseModel):
    """Everything a source is allowed to know about one probe."""

    investigation_id: str
    source: str
    kind: SourceKind
    time_range: TimeRange | None = None
    resource_targets: list[ResourceRef] = Field(default_factory=list)
    scenario: str = ""  # fixture scenario_id when demo_mode
    demo_mode: bool = False


class ProbeResult(BaseModel):
    """Uniform probe envelope. Sources NEVER raise to the graph — failures are
    data, so the report can say 'skipped: athena — not authorized'."""

    evidence: list[EvidenceItem] = Field(default_factory=list)
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    failures: list[str] = Field(default_factory=list)
    skipped_reason: str = ""
