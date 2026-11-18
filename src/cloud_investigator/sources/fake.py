"""FakeSource — cassette-backed read-only source adapter (offline spike).

The point of this adapter is to prove the pinned base contract end-to-end
with zero cloud access:

- Even canned fixture operations must pass ``assert_read_only`` — the
  chokepoint has no demo bypass.
- Evidence ids are minted ONLY here, via ``make_evidence_id`` (never by an
  LLM node).
- Every excerpt runs through ``cap_excerpt`` — no unbounded payloads.
- A fixture 'error' comes back as *data* in the ProbeResult envelope
  (status=error tool call + failures list), never as an exception into the
  graph.
"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

from ..contracts import EvidenceItem, TimeRange
from .base import (
    ProbeContext,
    ProbeResult,
    ToolCallRecord,
    assert_read_only,
    cap_excerpt,
    make_evidence_id,
)


def _fixtures_dir() -> Path:
    """Locate fixtures/ — env override, then CWD, then repo layout."""
    override = os.environ.get("CLOUD_INVESTIGATOR_FIXTURES")
    if override:
        return Path(override)
    local = Path.cwd() / "fixtures"
    if local.is_dir():
        return local
    return Path(__file__).resolve().parents[3] / "fixtures"


@lru_cache(maxsize=8)
def load_cassette(scenario: str) -> dict:
    path = _fixtures_dir() / f"cassette_{scenario}.json"
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


class FakeSource:
    """One adapter per cassette probe section; ``name`` is a SourceKind."""

    def __init__(self, name: str, scenario: str) -> None:
        self.name = name
        self.scenario = scenario
        self._calls = load_cassette(scenario)["probes"][name]["calls"]

    def probe(self, ctx: ProbeContext) -> ProbeResult:
        evidence: list[EvidenceItem] = []
        tool_calls: list[ToolCallRecord] = []
        failures: list[str] = []

        for call in self._calls:
            # The one auditable chokepoint — fixtures are NOT exempt.
            assert_read_only(call["operation"])

            record = ToolCallRecord(
                source=self.name,
                operation=call["operation"],
                params=call.get("params", {}),
                status=call.get("status", "ok"),
                duration_ms=call.get("duration_ms", 0),
                error=call.get("error", ""),
            )
            tool_calls.append(record)
            if record.status != "ok":
                # Verbatim upstream error as data (NO_DATA/ERROR ≠ exception).
                failures.append(f"{self.name}: {record.operation} → {record.error}")
                continue

            for ev in call.get("evidence", []):
                excerpt, truncated = cap_excerpt(ev["excerpt"])
                evidence.append(
                    EvidenceItem(
                        id=make_evidence_id(ctx.scenario, self.name, ev["seq"]),
                        kind=ctx.kind,
                        resource=ctx.resource_targets[0] if ctx.resource_targets else None,
                        query=ev.get("query", record.operation),
                        ts=TimeRange(**ev["ts"]) if ev.get("ts") else None,
                        excerpt=excerpt,
                        raw_ref=f"cassette:{ctx.scenario}:{self.name}#{ev['seq']}",
                        truncated=truncated,
                    )
                )

        return ProbeResult(evidence=evidence, tool_calls=tool_calls, failures=failures)


#: Source adapters connected in the spike (name == SourceKind literal).
_REGISTERED = ("cloudwatch_logs", "sql")


def registered_sources() -> tuple[str, ...]:
    return _REGISTERED


def make_source(name: str, scenario: str) -> FakeSource:
    if name not in _REGISTERED:
        raise KeyError(f"source adapter {name!r} is not registered")
    return FakeSource(name, scenario)
