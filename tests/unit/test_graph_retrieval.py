"""The retrieval stage: findings are grounded in the corpus, not the cassette."""

from __future__ import annotations

import json
from pathlib import Path

from cloud_investigator.engine.graph import (
    RETRIEVAL_TOP_N,
    _finding_query,
    run_investigation,
)
from cloud_investigator.retrieval.index import LocalRunbookIndex, default_runbook_index

GROUNDED = [
    "s3_access_denied",
    "rds_connection_refused",
    "ecs_task_oom",
    "alb_unhealthy_targets",
    "poisoned_logs",
    "rds_connection_exhaustion",
    "alb_target_port_mismatch",
]

#: scenario -> the runbook the catalog binds to its service
BOUND_RUNBOOK = {
    "s3_access_denied": "s3-access-denied.md",
    "rds_connection_refused": "rds-connection-exhaustion.md",
    "ecs_task_oom": "ecs-task-oom.md",
    "alb_unhealthy_targets": "alb-5xx-target-health.md",
    "poisoned_logs": "alb-5xx-target-health.md",
    "rds_connection_exhaustion": "rds-connection-exhaustion.md",
    "alb_target_port_mismatch": "alb-5xx-target-health.md",
}


class _EmptyIndex:
    """A retriever that finds nothing — the empty-corpus case."""

    def retrieve_citations(self, query: str) -> list:
        return []


def test_every_finding_is_grounded_in_a_real_chunk() -> None:
    known = set(default_runbook_index().chunk_ids)
    for scenario in GROUNDED:
        report = run_investigation(scenario=scenario)
        assert report.findings, scenario
        assert report.citations, f"{scenario}: no citation reached the report"
        for citation in report.citations:
            assert citation.chunk_id in known, (
                f"{scenario}: citation {citation.chunk_id!r} is not a chunk of the corpus"
            )
            assert citation.excerpt, f"{scenario}: citation without an excerpt"


def test_findings_cite_the_runbook_the_catalog_binds_to_the_service() -> None:
    for scenario in GROUNDED:
        report = run_investigation(scenario=scenario)
        expected = BOUND_RUNBOOK[scenario]
        for finding in report.findings:
            assert finding.citations, f"{scenario}: finding {finding.title!r} lost its grounding"
            for citation in finding.citations:
                assert citation.source.endswith(expected), (
                    f"{scenario}: finding {finding.title!r} cited {citation.source!r}, "
                    f"expected the service runbook {expected}"
                )


def test_retrieved_citations_replace_the_authored_ones() -> None:
    cassette = json.loads(
        Path("fixtures/cassette_s3_access_denied.json").read_text(encoding="utf-8")
    )
    authored = {
        c["chunk_id"]
        for finding in cassette["demo_script"].get("findings", [])
        for c in finding.get("citations", [])
    }
    assert authored, "fixture sanity: the cassette ships authored citations"
    report = run_investigation(scenario="s3_access_denied")
    retrieved = {c.chunk_id for c in report.citations}
    assert retrieved, "retrieval produced nothing for a corpus-backed scenario"
    assert retrieved - authored, "citations were passed through instead of retrieved"
    # One authored id points at a section that does not exist in the runbook.
    assert "rb:s3-access-denied:s3-access-denied-iam-policy-diagnosis:94d8e3b6" not in retrieved


def test_empty_index_falls_back_to_the_analyst_citations() -> None:
    import cloud_investigator.engine.graph as graph_module

    empty = _EmptyIndex()
    report = graph_module.build_graph(index=empty).invoke(
        graph_module.initial_state("", "s3_access_denied")
    )["report"]
    assert report.findings
    assert report.citations, "an empty index must not strip the finding's own citations"


def test_abstention_cites_nothing() -> None:
    report = run_investigation(scenario="insufficient_evidence")
    assert report.abstained
    assert report.citations == []


def test_citations_are_identical_across_runs() -> None:
    first = run_investigation(scenario="alb_unhealthy_targets")
    second = run_investigation(scenario="alb_unhealthy_targets")
    assert [(c.chunk_id, c.score) for c in first.citations] == [
        (c.chunk_id, c.score) for c in second.citations
    ]


def test_citation_budget_is_bounded() -> None:
    report = run_investigation(scenario="s3_access_denied")
    for finding in report.findings:
        assert len(finding.citations) <= RETRIEVAL_TOP_N


def test_finding_query_is_built_from_the_report_itself() -> None:
    report = run_investigation(scenario="rds_connection_exhaustion")
    finding = report.findings[0]
    query = _finding_query(finding, "rds-orders")
    assert finding.title in query
    assert "Connection Pool Exhausted" in query, "the cause token is split for retrieval"
    assert "rds-orders" in query
    assert len(query) <= 400


def test_index_sees_the_same_corpus_the_ingest_pipeline_writes() -> None:
    index = LocalRunbookIndex("runbooks")
    assert set(index.chunk_ids) == set(default_runbook_index().chunk_ids)
