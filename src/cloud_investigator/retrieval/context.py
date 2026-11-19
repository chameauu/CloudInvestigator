# Vendored from lellisz/incident-copilot (Apache-2.0), commit 3b7e761,
# src/incident_copilot/rag/context.py. Modifications: none (verbatim lift;
# imports RetrievedChunk from the adapted retriever.py, same model shape).
# See ATTRIBUTION.md and third_party/incident-copilot/.
"""Format retrieved chunks into a cited context string."""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel

from .retriever import RetrievedChunk


class Citation(BaseModel):
    marker: int
    chunk_id: str
    doc_id: str
    heading_path: str


def build_context(chunks: Sequence[RetrievedChunk]) -> tuple[str, list[Citation]]:
    parts: list[str] = []
    citations: list[Citation] = []
    for i, c in enumerate(chunks, start=1):
        parts.append(f"[{i}] {c.heading_path}\n{c.content}")
        citations.append(
            Citation(
                marker=i,
                chunk_id=c.chunk_id,
                doc_id=c.doc_id,
                heading_path=c.heading_path,
            )
        )
    return "\n\n".join(parts), citations
