"""Integration: runbook RAG against the running compose stack (`make up` first).

Ingests the real runbooks corpus, then proves hybrid retrieval (pgvector +
FTS + RRF) returns grounded citations for the demo queries. The shared `db_up`
fixture fails loudly — never skips — when the stack is down or the schema is
missing, so those states are failures, not silences. These runs are deferred
until the store lane's stack + migrations are up; they are not part of the
offline unit gate.
"""

from __future__ import annotations

import pytest

from cloud_investigator import config
from cloud_investigator.retrieval.config import retrieval_config
from cloud_investigator.retrieval.embeddings import FakeEmbedder
from cloud_investigator.retrieval.ingest import RunbookIngestor
from cloud_investigator.retrieval.retriever import RunbookRetriever

pytestmark = pytest.mark.integration

#: query -> the runbook expected to win the fused ranking
EXPECTED_TOP = {
    "S3 AccessDenied cause": "s3-access-denied.md",
    "database too many connections": "rds-connection-exhaustion.md",
}


@pytest.fixture(scope="module")
def ingested(db_up):
    settings = config.load_settings()
    conn = db_up()
    try:
        ingestor = RunbookIngestor(
            conn,
            embedder=FakeEmbedder(dim=settings.embedding_dim),
            env=settings.environment,
        )
        stats = ingestor.ingest_dir(settings.runbook_dir)
    finally:
        conn.close()
    return stats


@pytest.fixture(scope="module")
def retriever(db_up):
    settings = config.load_settings()
    cfg = retrieval_config(settings)
    conn = db_up()
    try:
        yield RunbookRetriever(
            conn,
            embedder=FakeEmbedder(dim=cfg.embedding_dim),
            top_k=cfg.top_k,
            candidates=cfg.candidates_per_channel,
            rrf_k=cfg.rrf_k,
        )
    finally:
        conn.close()


def test_ingest_covers_the_corpus(ingested) -> None:
    assert ingested.files == 4, "expected the four ops runbooks"
    assert ingested.chunks_seen >= 4
    assert ingested.chunks_upserted == ingested.chunks_seen


@pytest.mark.parametrize("query, expected", sorted(EXPECTED_TOP.items()))
def test_query_returns_citations_from_the_right_runbook(retriever, query, expected) -> None:
    citations = retriever.retrieve_citations(query)
    assert citations, f"expected >= 1 citation for {query!r}"
    top = citations[0]
    assert top.source.endswith(expected), (
        f"top citation for {query!r} came from {top.source!r}, expected {expected}"
    )
    assert top.chunk_id.startswith("rb:")
    assert top.section, "citations must carry a heading breadcrumb"
    assert top.excerpt, "citations must carry a content excerpt"


def test_hybrid_fusion_returns_runbook_chunks(retriever) -> None:
    chunks = retriever.retrieve("S3 bucket policy explicit deny diagnosis")
    assert chunks, "hybrid fusion returned nothing for a corpus-backed query"
    for chunk in chunks:
        assert chunk.chunk_id.startswith("rb:")
        assert chunk.doc_id.endswith(".md")
        assert chunk.score > 0
        assert chunk.content


def test_rerank_offline_path_keeps_rrf_order(retriever) -> None:
    from cloud_investigator.retrieval.rerank import rerank

    chunks = retriever.retrieve("S3 AccessDenied cause")
    assert chunks
    scored = rerank("S3 AccessDenied cause", chunks, top_n=len(chunks))
    assert [c.chunk_id for c in scored] == [c.chunk_id for c in chunks]
    # Default top_n is the contract's rerank budget (4): RRF order, truncated.
    assert len(rerank("S3 AccessDenied cause", chunks)) == min(4, len(chunks))
