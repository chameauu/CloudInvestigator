"""Rerank retrieved chunks for a query — offline deterministic by default.

`scorer=None` returns the candidates in their incoming order, which is the
RRF order the hybrid retriever produced; their fused score is carried as
`rerank_score`. An injected scorer reorders them and MUST be one batched call
over all candidates, never one call per candidate. No LLM/router/LangChain
imports: a real scorer plugs in later as a plain callable.
"""

from __future__ import annotations

from collections.abc import Callable

from pydantic import BaseModel

from .retriever import RetrievedChunk

Scorer = Callable[[str, list[RetrievedChunk]], list[float]]


class ScoredChunk(BaseModel):
    """A retrieved chunk plus the rerank score that decided its position."""

    chunk_id: str
    doc_id: str
    heading_path: str
    content: str
    score: float
    rerank_score: float


def rerank(
    query: str,
    candidates: list[RetrievedChunk],
    scorer: Scorer | None = None,
    top_n: int = 4,
) -> list[ScoredChunk]:
    """Return up to `top_n` candidates, best first.

    Offline path (`scorer=None`): keep RRF order, fused score as rerank score.
    Injected path: exactly one batched `scorer(query, candidates)` call; equal
    scores keep input order (stable sort).
    """
    if not candidates:
        return []
    if scorer is None:
        scored = [ScoredChunk(**c.model_dump(), rerank_score=c.score) for c in candidates]
        return scored[:top_n]

    raw = scorer(query, candidates)
    if len(raw) != len(candidates):
        raise ValueError(f"scorer returned {len(raw)} scores for {len(candidates)} candidates")
    order = sorted(range(len(candidates)), key=lambda i: (-float(raw[i]), i))
    return [
        ScoredChunk(**candidates[i].model_dump(), rerank_score=float(raw[i])) for i in order[:top_n]
    ]
