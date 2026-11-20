"""Replay determinism: the same cassette twice must produce the same report.

Two fields are tolerated differences: ``created_at`` records when the report was
built, and ``investigation_id`` is minted per run — two independent executions
are two investigations by design. Everything else must match byte-for-byte.
"""

from __future__ import annotations

from typing import Any

from ..contracts import InvestigationReport

_RUN_LOCAL_FIELDS = ("created_at", "investigation_id")


def report_fingerprint(report: InvestigationReport) -> dict[str, Any]:
    """Full report as JSON-safe data, with run-local identity/time removed."""
    data = report.model_dump(mode="json")
    for field in _RUN_LOCAL_FIELDS:
        data.pop(field, None)
    return data


def same_report(left: InvestigationReport, right: InvestigationReport) -> bool:
    """True when two runs produced identical findings, evidence and wording."""
    return report_fingerprint(left) == report_fingerprint(right)
