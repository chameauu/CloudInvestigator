"""Runbook index seam — retrieval as a pipeline stage.

Two backends, one interface:

- ``LocalRunbookIndex`` chunks ``settings.runbook_dir`` with the same chunker
  and the same deterministic chunk ids the ingest pipeline uses, and ranks
  chunks with the offline ``FakeEmbedder`` cosine channel plus a lexical AND
  channel, fused by the same RRF. No database, no network: this is the default
  backend, so ``scripts/smoke.py``, ``make eval`` and the API stay
  deterministic and DB-free.
- ``PostgresRunbookIndex`` wraps ``RunbookRetriever`` (pgvector + ``ts_rank``)
  behind the same ``retrieve_citations`` call for runs that do have the
  ledger database up.

Either way the citations are *minted from the corpus*: every ``chunk_id`` is
``chunk_id_for(...)`` of a chunk that exists on disk, so a report citation can
be re-derived instead of trusted. The lexical channel is a simplification of
Postgres ``plainto_tsquery`` (all terms present, no stemming); the vector
channel is the same math over the same embeddings. The parity test in
``tests/integration/test_retrieval.py`` pins agreement on the runbook and
section that ground each signature query — the exact chunk can differ inside a
section the chunker split.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from functools import lru_cache
from pathlib import Path
from typing import Protocol

import psycopg

from ..config import Settings, load_settings
from ..contracts import Citation
from .chunker import chunk_markdown
from .embeddings import Embedder, FakeEmbedder
from .fusion import rrf
from .ingest import chunk_id_for
from .retriever import RetrievedChunk, RunbookRetriever, to_citations

log = logging.getLogger(__name__)

_WORD = re.compile(r"[a-z0-9]+")

# Deliberately small: the lexical channel only needs to reject the terms that
# carry no retrieval signal. Postgres does the real stemming in the DB backend.
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "been",
        "before",
        "but",
        "by",
        "can",
        "could",
        "did",
        "do",
        "does",
        "for",
        "from",
        "had",
        "has",
        "have",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "our",
        "so",
        "than",
        "that",
        "the",
        "their",
        "then",
        "there",
        "these",
        "they",
        "this",
        "to",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "will",
        "with",
        "you",
        "your",
    }
)


class RunbookIndex(Protocol):
    """Anything that can ground a query in runbook chunks."""

    def retrieve_citations(self, query: str) -> list[Citation]: ...


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def resolve_corpus(runbook_dir: str | Path) -> Path | None:
    """Resolve the corpus directory: as given, then relative to the repo root.

    ``settings.runbook_dir`` is ``"runbooks"`` — a repo-relative default, so a
    process started from a subdirectory still finds the corpus.
    """
    candidate = Path(runbook_dir)
    if candidate.is_dir():
        return candidate
    alt = _repo_root() / candidate
    return alt if alt.is_dir() else None


def _dot(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


class LocalRunbookIndex:
    """Offline hybrid index over the runbook corpus.

    The index is built once per corpus: chunking, ids and embeddings are all
    deterministic, so two processes rank identically and a replay cannot drift.
    A missing or empty corpus yields an empty index (retrieval is grounding,
    not a dependency — an investigation still answers, with fewer citations).
    """

    def __init__(
        self,
        runbook_dir: str | Path,
        *,
        embedder: Embedder | None = None,
        top_k: int = 8,
        candidates: int = 20,
        rrf_k: int = 60,
    ) -> None:
        self._embedder = embedder or FakeEmbedder()
        self._top_k = top_k
        self._candidates = candidates
        self._rrf_k = rrf_k
        self._ids: list[str] = []
        self._chunks: dict[str, RetrievedChunk] = {}
        self._vectors: list[list[float]] = []
        self._tokens: list[set[str]] = []
        self._load(Path(runbook_dir))

    # ------------------------------------------------------------- building

    def _load(self, root: Path) -> None:
        if not root.is_dir():
            log.warning("runbook corpus not found at %s — index is empty", root)
            return
        doc_base = root.as_posix().rstrip("/")
        for path in sorted(root.glob("*.md")):
            text = path.read_text(encoding="utf-8")
            for chunk in chunk_markdown(text, path.stem):
                chunk_id = chunk_id_for(path.stem, chunk.heading_path, chunk.content)
                if chunk_id in self._chunks:  # identical text twice in one doc
                    continue
                self._ids.append(chunk_id)
                self._chunks[chunk_id] = RetrievedChunk(
                    chunk_id=chunk_id,
                    doc_id=f"{doc_base}/{path.name}",
                    heading_path=chunk.heading_path,
                    content=chunk.content,
                    score=0.0,
                )
                self._tokens.append(set(_WORD.findall(chunk.content.lower())))
        if self._ids:
            self._vectors = self._embedder.embed([self._chunks[cid].content for cid in self._ids])

    # ------------------------------------------------------------ retrieval

    def __len__(self) -> int:
        return len(self._ids)

    @property
    def chunk_ids(self) -> list[str]:
        return list(self._ids)

    def _lexical(self, query: str) -> list[str]:
        """Rank chunks containing every query term (a stand-in for plainto_tsquery).

        Occurrence count breaks ties, then corpus order, so the channel is a
        pure function of the query and the corpus.
        """
        terms = list(dict.fromkeys(_WORD.findall(query.lower())))
        terms = [t for t in terms if len(t) > 1 and t not in _STOPWORDS]
        if not terms:
            return []
        scored: list[tuple[int, int, str]] = []
        for idx, tokens in enumerate(self._tokens):
            if not all(term in tokens for term in terms):
                continue
            body = self._chunks[self._ids[idx]].content.lower()
            occurrences = sum(1 for word in _WORD.findall(body) if word in terms)
            scored.append((-occurrences, idx, self._ids[idx]))
        scored.sort()
        return [cid for _score, _idx, cid in scored]

    def retrieve(self, query: str) -> list[RetrievedChunk]:
        if not self._ids or not query.strip():
            return []
        qvec = self._embedder.embed([query])[0]
        vec_order = sorted(
            range(len(self._ids)),
            key=lambda i: (-_dot(qvec, self._vectors[i]), i),
        )[: self._candidates]
        vec_ids = [self._ids[i] for i in vec_order]
        fused = rrf([vec_ids, self._lexical(query)], k=self._rrf_k, top_n=self._top_k)
        return [
            self._chunks[cid].model_copy(update={"score": score})
            for cid, score in fused
            if cid in self._chunks
        ]

    def retrieve_citations(self, query: str) -> list[Citation]:
        return to_citations(self.retrieve(query))


class PostgresRunbookIndex:
    """The pgvector + full-text backend, behind the same interface."""

    def __init__(self, conn: psycopg.Connection, settings: Settings | None = None) -> None:
        s = settings or load_settings()
        self._retriever = RunbookRetriever(
            conn,
            FakeEmbedder(s.embedding_dim),
            top_k=8,
            candidates=20,
            rrf_k=60,
        )

    def retrieve_citations(self, query: str) -> list[Citation]:
        return self._retriever.retrieve_citations(query)


@lru_cache(maxsize=4)
def _local_index(corpus: str, embedding_dim: int) -> LocalRunbookIndex:
    return LocalRunbookIndex(corpus, embedder=FakeEmbedder(embedding_dim))


def default_runbook_index(settings: Settings | None = None) -> LocalRunbookIndex:
    """The offline index the pipeline uses unless a caller injects another.

    Cached per corpus path: chunking and embedding the corpus once is enough,
    and callers never see a half-built index.
    """
    s = settings or load_settings()
    resolved = resolve_corpus(s.runbook_dir)
    corpus = resolved.as_posix() if resolved is not None else ""
    return _local_index(corpus, s.embedding_dim)
