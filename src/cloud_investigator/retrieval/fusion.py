# Vendored from lellisz/incident-copilot (Apache-2.0), commit 3b7e761,
# src/incident_copilot/rag/fusion.py. Modifications: none (verbatim lift).
# See ATTRIBUTION.md and third_party/incident-copilot/.
"""Reciprocal Rank Fusion for combining multiple ranked result lists."""

from __future__ import annotations


def rrf(
    rankings: list[list[str]],
    k: int = 60,
    top_n: int = 8,
) -> list[tuple[str, float]]:
    """Fuse ranked lists of ids into one. Score = sum(1 / (k + rank)), rank 0-based.

    Ties broken by first appearance across the input rankings (stable).
    """
    scores: dict[str, float] = {}
    first_seen: dict[str, int] = {}
    order = 0
    for ranking in rankings:
        for rank, item in enumerate(ranking):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank)
            if item not in first_seen:
                first_seen[item] = order
                order += 1
    fused = sorted(scores.items(), key=lambda kv: (-kv[1], first_seen[kv[0]]))
    return fused[:top_n]
