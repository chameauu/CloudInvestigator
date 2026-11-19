"""Replay determinism: the same cassette twice must produce the same report.

``created_at`` is the only tolerated difference — it records when the report
object was built, not what the investigation found.
"""

from __future__ import annotations

from typing import Any

from ..contracts import InvestigationReport


def report_fingerprint(report: InvestigationReport) -> dict[str, Any]:
    """Full report as JSON-safe data, with the run timestamp removed."""
    data = report.model_dump(mode="json")
    data.pop("created_at", None)
    return data


def same_report(left: InvestigationReport, right: InvestigationReport) -> bool:
    """True when two runs produced identical findings, evidence and wording."""
    return report_fingerprint(left) == report_fingerprint(right)
