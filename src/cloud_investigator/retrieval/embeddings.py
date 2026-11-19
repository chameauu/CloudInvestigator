"""Deterministic offline embedders for the runbook RAG pillar.

CloudInvestigator is offline-first: no network, no cloud credentials, no API
keys. The default embedder is a deterministic blake2b bag-of-words hash — the
idea kept from incident-copilot's `_fake()` CI embedder, rewritten here. It is
not semantically meaningful, but it is stable: the same text always maps to the
same vector, so retrieval behaves like a regression test. A real embedder plugs
in later behind the `Embedder` protocol; the vector dimension is always
injected by the caller (`settings.embedding_dim`, never a module singleton).
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Protocol

_WORD_RE = re.compile(r"[a-z0-9]+")


class Embedder(Protocol):
    """Anything that turns texts into fixed-dimension float vectors."""

    def embed(self, texts: list[str]) -> list[list[float]]: ...


def format_vector(vector: list[float]) -> str:
    """Render a vector as a pgvector literal, e.g. `[0.1,0.2,...]`.

    The pgvector Python package is not a project dependency, so embeddings
    travel to Postgres as SQL text and are cast with `::vector` at the call
    site. `repr` round-trips Python floats exactly.
    """
    return "[" + ",".join(repr(x) for x in vector) + "]"


class FakeEmbedder:
    """Deterministic bag-of-words hash embedder (the offline default).

    Each word hashes (blake2b) to one dimension and counts occurrences; the
    vector is then L2-normalized. Properties: same text -> identical vector,
    always; word order does not matter; no semantic signal. Good enough to
    exercise the retrieval pipeline end-to-end without any network.
    """

    def __init__(self, dim: int = 1024) -> None:
        if dim <= 0:
            raise ValueError(f"dim must be positive, got {dim}")
        self._dim = dim

    @property
    def dim(self) -> int:
        return self._dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        vec = [0.0] * self._dim
        for word in _WORD_RE.findall(text.lower()):
            digest = hashlib.blake2b(word.encode("utf-8"), digest_size=8).digest()
            vec[int.from_bytes(digest, "big") % self._dim] += 1.0
        norm = math.sqrt(sum(x * x for x in vec)) or 1.0
        return [x / norm for x in vec]
