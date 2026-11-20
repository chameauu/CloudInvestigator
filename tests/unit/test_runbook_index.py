"""Offline runbook index: the retrieval stage's default backend.

No database and no network — the index chunks the real corpus with the same
chunker and ids the ingest pipeline uses, so what it cites is re-derivable.
"""

from __future__ import annotations

import pytest

from cloud_investigator.config import load_settings
from cloud_investigator.retrieval.index import (
    LocalRunbookIndex,
    default_runbook_index,
    resolve_corpus,
)
from cloud_investigator.retrieval.ingest import chunk_id_for

#: query -> the runbook the top citation must come from
SIGNATURE_QUERIES = {
    "S3 AccessDenied IAM policy bucket": "s3-access-denied.md",
    "connection pool exhausted postgres too many clients": "rds-connection-exhaustion.md",
    "target group unhealthy 502 503": "alb-5xx-target-health.md",
    "container OOM killed exit code 137": "ecs-task-oom.md",
}


def test_index_covers_the_shipped_corpus() -> None:
    index = LocalRunbookIndex(load_settings().runbook_dir)
    assert len(index) >= 9, "the four runbooks chunk into at least nine chunks"
    ids = index.chunk_ids
    assert len(ids) == len(set(ids)), "chunk ids must be unique"
    assert all(cid.startswith("rb:") for cid in ids)


@pytest.mark.parametrize("query, expected", sorted(SIGNATURE_QUERIES.items()))
def test_signature_query_cites_the_right_runbook(query: str, expected: str) -> None:
    index = LocalRunbookIndex(load_settings().runbook_dir)
    citations = index.retrieve_citations(query)
    assert citations, f"expected at least one citation for {query!r}"
    top = citations[0]
    assert top.source.endswith(expected)
    assert top.chunk_id.startswith("rb:")
    assert top.section, "citations must carry a heading breadcrumb"
    assert top.excerpt, "citations must carry a content excerpt"
    assert top.score > 0


def test_retrieval_is_deterministic_across_instances() -> None:
    query = "target group unhealthy 502 503"
    first = LocalRunbookIndex(load_settings().runbook_dir).retrieve_citations(query)
    second = LocalRunbookIndex(load_settings().runbook_dir).retrieve_citations(query)
    assert [c.model_dump() for c in first] == [c.model_dump() for c in second]


def test_citations_resolve_to_the_chunk_ids_ingest_would_write(tmp_path) -> None:
    corpus = tmp_path / "runbooks"
    corpus.mkdir()
    body = "# Disk full\n\nThe volume filled up.\n\n## Check\n\nRun df -h.\n"
    (corpus / "disk-full.md").write_text(body, encoding="utf-8")
    index = LocalRunbookIndex(corpus)
    assert len(index) == 1
    citations = index.retrieve_citations("disk full volume")
    assert citations
    # The id is a function of the same chunker + hash as the ingest path.
    from cloud_investigator.retrieval.chunker import chunk_markdown

    expected = {
        chunk_id_for("disk-full", chunk.heading_path, chunk.content)
        for chunk in chunk_markdown(body, "disk-full")
    }
    assert citations[0].chunk_id in expected


def test_missing_corpus_yields_an_empty_index(tmp_path) -> None:
    index = LocalRunbookIndex(tmp_path / "no-such-dir")
    assert len(index) == 0
    assert index.retrieve_citations("anything at all") == []


def test_blank_query_returns_nothing() -> None:
    index = LocalRunbookIndex(load_settings().runbook_dir)
    assert index.retrieve_citations("   ") == []


def test_default_index_resolves_the_repo_relative_corpus() -> None:
    settings = load_settings()
    assert resolve_corpus(settings.runbook_dir) is not None
    assert len(default_runbook_index(settings)) == len(LocalRunbookIndex(settings.runbook_dir))
