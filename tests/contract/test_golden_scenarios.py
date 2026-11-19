"""Contract: every golden scenario is replayable and internally consistent.

Offline, no containers. These checks fail when a cassette and its probe
cassettes drift apart — the failure mode that would silently invalidate an
eval score.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from cloud_investigator.catalog import load_catalog
from cloud_investigator.eval.golden import load_golden

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "fixtures"
PROBES = FIXTURES / "probes"

#: Sequence 999 is reserved for the documented nonexistent id: it exists so
#: verify's demotion path is exercised (the poisoned claim must never confirm).
POISON_SEQ = re.compile(r"^ev:[a-z0-9_]+:sql:999$")


def _scenarios():
    return load_golden(REPO_ROOT / "evals" / "golden.yaml")


def _cassette(name: str) -> dict:
    return json.loads((FIXTURES / f"cassette_{name}.json").read_text(encoding="utf-8"))


def _probe(name: str, source: str) -> dict:
    return json.loads((PROBES / f"{source}_{name}.json").read_text(encoding="utf-8"))


def _evidence_count(name: str, source: str) -> int:
    data = _probe(name, source)
    if source == "cloudwatch_logs":
        return len(data.get("events", []))
    return sum(len(query.get("rows", [])) for query in data.get("queries", []))


@pytest.mark.parametrize("scenario", _scenarios(), ids=lambda s: s.name)
def test_each_scenario_has_a_matching_cassette(scenario):
    cassette = _cassette(scenario.name)
    assert cassette["scenario_id"] == scenario.name
    assert cassette["question"].strip()
    assert cassette["demo_script"]["service"]


@pytest.mark.parametrize("scenario", _scenarios(), ids=lambda s: s.name)
def test_planned_sources_have_probe_cassettes(scenario):
    cassette = _cassette(scenario.name)
    for source in cassette["demo_script"]["plan"]["sources"]:
        assert (PROBES / f"{source}_{scenario.name}.json").exists(), (
            f"{scenario.name}: missing probe cassette for source {source}"
        )


@pytest.mark.parametrize("scenario", _scenarios(), ids=lambda s: s.name)
def test_probe_scope_matches_the_catalog_binding(scenario):
    cassette = _cassette(scenario.name)
    service = cassette["demo_script"]["service"]
    catalog = load_catalog(REPO_ROOT / "catalog.yaml")
    entry = catalog.services[service]
    for source, binding in entry.sources.items():
        path = PROBES / f"{source}_{scenario.name}.json"
        if not path.exists():
            continue
        probe = json.loads(path.read_text(encoding="utf-8"))
        if source == "cloudwatch_logs":
            assert probe["log_group"] in binding.log_groups
            assert probe["filter_pattern"] == binding.filter_pattern
        else:
            assert probe["database"] in binding.databases


@pytest.mark.parametrize("scenario", _scenarios(), ids=lambda s: s.name)
def test_cited_evidence_ids_resolve_or_are_the_poison_marker(scenario):
    cassette = _cassette(scenario.name)
    script = cassette["demo_script"]
    available = {
        source: _evidence_count(scenario.name, source) for source in script["plan"]["sources"]
    }

    cited: list[str] = []
    for hypothesis in script.get("hypotheses", []):
        cited.extend(hypothesis.get("evidence_ids", []))
    for finding in script.get("findings", []):
        cited.extend(finding.get("evidence_ids", []))

    for evidence_id in cited:
        if POISON_SEQ.match(evidence_id):
            continue
        match = re.fullmatch(rf"ev:{re.escape(scenario.name)}:([a-z_]+):(\d+)", evidence_id)
        assert match, f"{scenario.name}: malformed evidence id {evidence_id!r}"
        source, seq = match.group(1), int(match.group(2))
        assert source in available, f"{scenario.name}: cites unplanned source {source}"
        assert 1 <= seq <= available[source], (
            f"{scenario.name}: {evidence_id} exceeds the {available[source]} "
            f"evidence item(s) the {source} probe can mint"
        )


@pytest.mark.parametrize("scenario", _scenarios(), ids=lambda s: s.name)
def test_competing_candidates_are_declared(scenario):
    script = _cassette(scenario.name)["demo_script"]
    hypotheses = script.get("hypotheses", [])
    if scenario.expectation.abstain:
        assert hypotheses == [], "an abstention scenario must not script causes"
        return
    assert len(hypotheses) >= 2, f"{scenario.name}: the eval expects competing candidates"


def test_poisoned_scenario_really_contains_an_injection():
    script = _cassette("poisoned_logs")["demo_script"]
    marked = [
        evidence_id
        for hypothesis in script["hypotheses"]
        for evidence_id in hypothesis.get("evidence_ids", [])
        if POISON_SEQ.match(evidence_id)
    ]
    assert marked, "poisoned_logs must cite the nonexistent id so verify exercises demotion"
    probe = _probe("poisoned_logs", "cloudwatch_logs")
    injected = [event for event in probe["events"] if "INVESTIGATION ASSISTANT" in event["message"]]
    assert injected, "poisoned_logs probe cassette must carry the injected instruction"
