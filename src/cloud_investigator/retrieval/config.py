"""Retrieval parameters, mapped from Settings — never a module singleton.

Callers parse configuration once (`config.load_settings()`) and pass the
result down. Nothing in this module reads the environment, and importing it
has no side effects.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import Settings


@dataclass(frozen=True)
class RetrievalConfig:
    """Knobs for the runbook RAG pillar (hybrid retrieval + rerank defaults)."""

    embedding_dim: int = 1024
    top_k: int = 8
    candidates_per_channel: int = 20
    rrf_k: int = 60
    rerank_top_n: int = 4


def retrieval_config(settings: Settings) -> RetrievalConfig:
    """Map the frozen Settings onto the retrieval knobs (dimension follows
    `settings.embedding_dim`; the rest are pillar defaults)."""
    return RetrievalConfig(embedding_dim=settings.embedding_dim)
