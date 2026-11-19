# Vendored from lellisz/incident-copilot (Apache-2.0), commit 3b7e761,
# src/incident_copilot/rag/retriever.py. Modifications: adapted to the
# runbook_chunks schema (chunk_id TEXT, doc_path/section_path columns, FTS
# 'english' via the generated tsv column); settings/embedder injected instead
# of module globals; no structlog/metrics, no pgvector package. See
# ATTRIBUTION.md and third_party/incident-copilot/.
"""Hybrid retriever: vector (cosine) + full-text (ts_rank), fused with RRF.

Target schema (migrations/002_runbooks.sql, owned by the store lane):

    runbook_chunks(
        chunk_id TEXT UNIQUE, doc_path, section_path, service_id, env,
        updated_at, content, embedding vector(1024),
        tsv GENERATED ALWAYS AS to_tsvector('english', content))
    with HNSW (vector_cosine_ops) on embedding and GIN on tsv.

Embeddings travel as text cast `::vector` (no pgvector Python dependency).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

import psycopg
from pydantic import BaseModel

from ..contracts import Citation
from ..sources.base import EXCERPT_MAX_CHARS, cap_excerpt
from .embeddings import Embedder, format_vector
from .fusion import rrf

log = logging.getLogger(__name__)

VECTOR_SQL = """
SELECT chunk_id
FROM runbook_chunks
ORDER BY embedding <=> %s::vector
LIMIT %s
"""

FTS_SQL = """
SELECT chunk_id
FROM runbook_chunks
WHERE tsv @@ plainto_tsquery('english', %s)
ORDER BY ts_rank(tsv, plainto_tsquery('english', %s)) DESC
LIMIT %s
"""

HYDRATE_SQL = """
SELECT chunk_id, doc_path, section_path, content
FROM runbook_chunks
WHERE chunk_id = ANY(%s)
"""


class RetrievedChunk(BaseModel):
    """One fused retrieval hit, in RRF order."""

    chunk_id: str
    doc_id: str  # runbook doc path, e.g. runbooks/s3-access-denied.md
    heading_path: str  # heading breadcrumb of the chunk
    content: str
    score: float  # fused RRF score


def to_citations(
    chunks: Sequence[RetrievedChunk], max_chars: int = EXCERPT_MAX_CHARS
) -> list[Citation]:
    """Map retrieved chunks onto the report contract's Citation.

    `source` is the runbook doc path, `section` the heading breadcrumb, and
    the excerpt is capped like every other payload entering LLM context.
    """
    out: list[Citation] = []
    for c in chunks:
        excerpt, _truncated = cap_excerpt(c.content, max_chars)
        out.append(
            Citation(
                chunk_id=c.chunk_id,
                source=c.doc_id,
                section=c.heading_path,
                score=c.score,
                excerpt=excerpt,
            )
        )
    return out


class RunbookRetriever:
    """Hybrid retrieval over the runbook corpus.

    `embedder` must produce vectors of the dimension stored in
    runbook_chunks.embedding (`settings.embedding_dim`, 1024 by default).
    """

    def __init__(
        self,
        conn: psycopg.Connection,
        embedder: Embedder,
        top_k: int = 8,
        candidates: int = 20,
        rrf_k: int = 60,
    ) -> None:
        self._conn = conn
        self._embedder = embedder
        self._top_k = top_k
        self._candidates = candidates
        self._rrf_k = rrf_k

    def retrieve(self, query: str) -> list[RetrievedChunk]:
        """Vector channel + FTS channel, fused with RRF, hydrated in order."""
        qvec = format_vector(self._embedder.embed([query])[0])
        with self._conn.cursor() as cur:
            cur.execute(VECTOR_SQL, (qvec, self._candidates))
            vec_ids = [r[0] for r in cur.fetchall()]
            cur.execute(FTS_SQL, (query, query, self._candidates))
            fts_ids = [r[0] for r in cur.fetchall()]
            fused = rrf([vec_ids, fts_ids], k=self._rrf_k, top_n=self._top_k)
            if not fused:
                return []
            cur.execute(HYDRATE_SQL, ([cid for cid, _score in fused],))
            rows = {r[0]: r for r in cur.fetchall()}

        out = [
            RetrievedChunk(
                chunk_id=cid,
                doc_id=rows[cid][1],
                heading_path=rows[cid][2],
                content=rows[cid][3],
                score=score,
            )
            for cid, score in fused
            if cid in rows
        ]
        log.info("runbook retrieve: vec=%d fts=%d out=%d", len(vec_ids), len(fts_ids), len(out))
        return out

    def retrieve_citations(self, query: str) -> list[Citation]:
        """Retrieve, then map straight to the report contract's citations."""
        return to_citations(self.retrieve(query))
