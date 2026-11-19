"""FakeLLM — deterministic offline stand-in for the analyst LLM (Step 0 spike).

Mirrors the call surface a real model node would use — plan / hypotheses /
findings / summary — but every answer is canned structured data carried in the
`demo_script` dict (loaded from the cassette fixture by intake).

No API keys, no network, byte-identical on every call — INCLUDING verify's
reject-and-retry second call. That determinism is deliberate: it lets the
citation-ID guard prove its demote path offline (a poisoned claim stays
poisoned after one retry, so it must be demoted, never passed through).
"""

from __future__ import annotations

from ..contracts import Finding, Hypothesis, ProbePlan, ResourceRef, TimeRange


class FakeLLM:
    """Canned analyst. Constructed per node call from state['demo_script']."""

    def __init__(self, demo_script: dict) -> None:
        self._script = demo_script or {}

    def plan(self, question: str, service: str, time_range: TimeRange | None) -> ProbePlan:
        """Pick which sources to probe. A real LLM reads the question; the
        demo script answers deterministically."""
        spec = self._script.get("plan", {})
        return ProbePlan(
            sources=list(spec.get("sources", [])),
            time_range=time_range,
            resource_targets=[ResourceRef(**r) for r in self._script.get("resources", [])],
            rationale=spec.get("rationale", ""),
        )

    def hypotheses(self) -> list[Hypothesis]:
        """Competing ranked causes, some possibly citing ids that don't exist —
        verify is responsible for catching that, not the model."""
        return [Hypothesis(**h) for h in self._script.get("hypotheses", [])]

    def findings(self) -> list[Finding]:
        """Candidate findings with citations; verify decides which survive."""
        return [Finding(**f) for f in self._script.get("findings", [])]

    def summary(self) -> str:
        return self._script.get("summary", "")
