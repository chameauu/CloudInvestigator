"""Offline, deterministic unit tests for the runbook RAG pillar.

No network, no database, no clock: chunking, RRF math, context building,
citation mapping, the fake embedder, ingest upsert SQL (via a fake cursor),
the retriever's fusion wiring (via a fake cursor), rerank, and config mapping.
"""

from __future__ import annotations

import re

import pytest

from cloud_investigator.config import Settings
from cloud_investigator.contracts import Citation as ContractCitation
from cloud_investigator.retrieval.chunker import (
    MAX_TOKENS,
    MIN_TOKENS,
    chunk_markdown,
    estimate_tokens,
)
from cloud_investigator.retrieval.config import RetrievalConfig, retrieval_config
from cloud_investigator.retrieval.context import build_context
from cloud_investigator.retrieval.embeddings import FakeEmbedder, format_vector
from cloud_investigator.retrieval.fusion import rrf
from cloud_investigator.retrieval.ingest import (
    IngestStats,
    RunbookIngestor,
    chunk_id_for,
)
from cloud_investigator.retrieval.rerank import ScoredChunk, rerank
from cloud_investigator.retrieval.retriever import (
    FTS_SQL,
    HYDRATE_SQL,
    VECTOR_SQL,
    RetrievedChunk,
    RunbookRetriever,
    to_citations,
)

# ---------------------------------------------------------------- test helpers


def para(tokens: int) -> str:
    """A paragraph of roughly `tokens` estimated tokens (len // 4 heuristic)."""
    base = "diagnostic text about the incident window and what the on-call engineer saw. "
    text = base * (tokens * 4 // len(base) + 1)
    return text.rstrip()


def mkchunk(
    chunk_id: str = "rb:doc:sec:ab12cd34",
    doc_id: str = "runbooks/doc.md",
    heading_path: str = "A > B",
    content: str = "some runbook body text",
    score: float = 0.5,
) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id,
        doc_id=doc_id,
        heading_path=heading_path,
        content=content,
        score=score,
    )


class FakeCursor:
    """Records execute() calls; fetchall() drains a queue of canned rows."""

    def __init__(self, conn: FakeConn) -> None:
        self._conn = conn
        self.rowcount = 0

    def execute(self, sql: str, params: tuple = ()) -> None:
        self._conn.calls.append((sql, params))
        self.rowcount = 1

    def fetchall(self) -> list[tuple]:
        return self._conn.results.pop(0)

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class FakeConn:
    def __init__(self, results: list[list[tuple]] | None = None) -> None:
        self.calls: list[tuple[str, tuple]] = []
        self.results: list[list[tuple]] = list(results or [])
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def write_runbook(root, name: str, body_tokens: int = 300) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / name).write_text(f"# {name}\n\n## Diagnosis\n\n{para(body_tokens)}", encoding="utf-8")


# ------------------------------------------------------------------- chunker


def test_chunker_keeps_heading_breadcrumbs() -> None:
    doc = "# Runbook\n\n## Diagnosis\n\n### IAM\n\n" + para(250)
    chunks = chunk_markdown(doc, "doc")
    assert len(chunks) == 1
    assert chunks[0].heading_path == "Runbook > Diagnosis > IAM"
    assert chunks[0].doc_id == "doc"


def test_chunker_respects_token_bounds() -> None:
    doc = "\n\n".join(
        ["# Ops runbook"] + [f"## Section {i}\n\n{para(300)}" for i in ("one", "two", "three")]
    )
    chunks = chunk_markdown(doc, "doc")
    assert len(chunks) == 3
    for c in chunks:
        assert MIN_TOKENS <= estimate_tokens(c.content) <= MAX_TOKENS
    assert [c.heading_path for c in chunks] == [
        "Ops runbook > Section one",
        "Ops runbook > Section two",
        "Ops runbook > Section three",
    ]


def test_chunker_splits_oversized_sections_by_paragraph() -> None:
    body = "\n\n".join(para(430) for _ in range(3))
    doc = "# Big\n\n## Huge\n\n" + body
    chunks = chunk_markdown(doc, "doc")
    assert len(chunks) == 3
    for c in chunks:
        assert estimate_tokens(c.content) <= MAX_TOKENS
        assert c.heading_path == "Big > Huge"


def test_chunker_keeps_code_blocks_intact() -> None:
    doc = (
        "# SQL diagnosis\n\n"
        "Run this against the authorized audit database.\n\n"
        "```sql\n"
        "SELECT actor, decision, ts FROM iam_audit_events\n"
        "WHERE resource LIKE 'arn:aws:s3:::%' ORDER BY ts DESC;\n"
        "```\n"
    )
    chunks = chunk_markdown(doc, "rb")
    joined = "\n\n".join(c.content for c in chunks)
    assert "SELECT actor, decision, ts FROM iam_audit_events" in joined
    assert "```sql" in joined and "```" in joined


def test_chunker_is_deterministic() -> None:
    doc = "# T\n\n## S\n\n" + para(250)
    a = chunk_markdown(doc, "doc")
    b = chunk_markdown(doc, "doc")
    assert a == b


# --------------------------------------------------------------------- fusion


def test_rrf_known_fusion_order_and_scores() -> None:
    fused = rrf([["a", "b", "c"], ["b", "a", "d"]], k=60, top_n=8)
    assert [i for i, _ in fused] == ["a", "b", "c", "d"]
    assert fused[0][1] == pytest.approx(1 / 60 + 1 / 61)
    assert fused[1][1] == pytest.approx(1 / 61 + 1 / 60)
    assert fused[2][1] == pytest.approx(1 / 62)
    assert fused[3][1] == pytest.approx(1 / 62)


def test_rrf_breaks_ties_by_first_appearance() -> None:
    # a and b fuse to identical scores; a appeared first across the rankings.
    fused = rrf([["a", "b", "c"], ["b", "a", "d"]], k=60)
    assert fused[0][1] == fused[1][1]
    assert fused[0][0] == "a"
    # x and y appear once each with the same rank -> exact tie, x seen first.
    tie = rrf([["x"], ["y"]])
    assert [i for i, _ in tie] == ["x", "y"]
    assert tie[0][1] == tie[1][1]


def test_rrf_caps_top_n() -> None:
    fused = rrf([["a", "b", "c"]], top_n=2)
    assert [i for i, _ in fused] == ["a", "b"]


def test_rrf_empty_rankings() -> None:
    assert rrf([]) == []
    assert rrf([[], []]) == []


# ------------------------------------------------------------- context builder


def test_build_context_markers_match_citations() -> None:
    chunks = [
        mkchunk("rb:a:one:aaaa1111", "runbooks/a.md", "Top > One", "alpha content"),
        mkchunk("rb:a:two:bbbb2222", "runbooks/a.md", "Top > Two", "beta content"),
    ]
    text, cites = build_context(chunks)
    assert text.startswith("[1] Top > One\nalpha content")
    assert "[2] Top > Two" in text
    assert [c.marker for c in cites] == [1, 2]
    assert [c.chunk_id for c in cites] == ["rb:a:one:aaaa1111", "rb:a:two:bbbb2222"]
    assert cites[0].doc_id == "runbooks/a.md"
    assert cites[0].heading_path == "Top > One"


def test_build_context_empty() -> None:
    assert build_context([]) == ("", [])


def test_to_citations_maps_contract_fields_and_caps_excerpt() -> None:
    chunk = mkchunk("rb:a:one:aaaa1111", "runbooks/a.md", "Top > One", para(600), 0.75)
    cits = to_citations([chunk])
    assert len(cits) == 1
    c = cits[0]
    assert isinstance(c, ContractCitation)
    assert c.chunk_id == "rb:a:one:aaaa1111"
    assert c.source == "runbooks/a.md"
    assert c.section == "Top > One"
    assert c.score == 0.75
    assert c.excerpt.startswith(chunk.content[:100])
    assert len(c.excerpt) <= 2100
    assert "truncated" in c.excerpt


# ------------------------------------------------------------------- embedder


def test_fake_embedder_deterministic() -> None:
    emb = FakeEmbedder(dim=1024)
    v1 = emb.embed(["alpha beta gamma"])[0]
    v2 = emb.embed(["alpha beta gamma"])[0]
    assert v1 == v2


def test_fake_embedder_different_text_different_vector() -> None:
    emb = FakeEmbedder()
    v1 = emb.embed(["alpha beta"])[0]
    v2 = emb.embed(["delta epsilon"])[0]
    assert v1 != v2


def test_fake_embedder_dim_and_unit_norm() -> None:
    emb = FakeEmbedder(dim=1024)
    vec = emb.embed(["connection pool exhausted rds"])[0]
    assert len(vec) == 1024
    assert sum(x * x for x in vec) == pytest.approx(1.0)


def test_fake_embedder_bag_of_words_order_invariant() -> None:
    emb = FakeEmbedder()
    assert emb.embed(["alpha beta"])[0] == emb.embed(["beta alpha"])[0]


def test_fake_embedder_edges() -> None:
    emb = FakeEmbedder(dim=8)
    assert emb.embed([]) == []
    zero = emb.embed([""])[0]
    assert len(zero) == 8
    assert zero == [0.0] * 8


def test_format_vector_round_trips_without_spaces() -> None:
    vec = FakeEmbedder(dim=4).embed(["hello world"])[0]
    s = format_vector(vec)
    assert s.startswith("[") and s.endswith("]")
    assert " " not in s
    assert [float(x) for x in s[1:-1].split(",")] == vec


# ------------------------------------------------------------------- ingest


def test_chunk_id_is_deterministic_and_content_sensitive() -> None:
    a = chunk_id_for("s3-access-denied", "Diagnosis > IAM", "same body text")
    b = chunk_id_for("s3-access-denied", "Diagnosis > IAM", "same body text")
    assert a == b
    assert re.fullmatch(r"rb:s3-access-denied:diagnosis-iam:[0-9a-f]{8}", a)
    assert chunk_id_for("s3-access-denied", "Diagnosis > IAM", "other body") != a
    assert chunk_id_for("s3-access-denied", "Remediation", "same body text") != a


def test_ingest_upserts_with_on_conflict_chunk_id(tmp_path) -> None:
    write_runbook(tmp_path, "s3-access-denied.md")
    conn = FakeConn()
    ingestor = RunbookIngestor(conn, embedder=FakeEmbedder(dim=16), env="qa")
    stats = ingestor.ingest_dir(tmp_path)

    assert stats == IngestStats(files=1, chunks_seen=1, chunks_upserted=1)
    assert conn.commits == 1
    assert len(conn.calls) == 1
    sql, params = conn.calls[0]

    assert "INSERT INTO runbook_chunks" in sql
    assert "ON CONFLICT (chunk_id) DO UPDATE" in sql
    assert "%s::vector" in sql
    chunk_id, doc_path, section_path, service_id, env, _content, vec = params
    assert re.fullmatch(r"rb:s3-access-denied:[a-z0-9-]+:[0-9a-f]{8}", chunk_id)
    assert doc_path.endswith("/s3-access-denied.md")
    assert section_path
    assert service_id == "s3"
    assert env == "qa"
    assert vec.startswith("[") and vec.count(",") == 15  # dim 16


def test_ingest_is_idempotent_same_chunk_id(tmp_path) -> None:
    write_runbook(tmp_path, "ecs-task-oom.md")
    conn1, conn2 = FakeConn(), FakeConn()
    ingestor = RunbookIngestor(conn1, embedder=FakeEmbedder(dim=16))
    ingestor.ingest_dir(tmp_path)
    ingestor2 = RunbookIngestor(conn2, embedder=FakeEmbedder(dim=16))
    ingestor2.ingest_dir(tmp_path)
    assert conn1.calls[0][1][0] == conn2.calls[0][1][0]
    assert conn2.commits == 1


def test_ingest_doc_path_uses_relative_corpus_root(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    write_runbook(tmp_path / "runbooks", "rds-connection-exhaustion.md")
    conn = FakeConn()
    ingestor = RunbookIngestor(conn, embedder=FakeEmbedder(dim=16))
    stats = ingestor.ingest_dir("runbooks")
    assert stats.files == 1
    _, params = conn.calls[0]
    assert params[1] == "runbooks/rds-connection-exhaustion.md"
    assert params[3] == "rds"


def test_ingest_empty_dir_returns_zero_stats(tmp_path) -> None:
    conn = FakeConn()
    stats = RunbookIngestor(conn, embedder=FakeEmbedder(dim=16)).ingest_dir(tmp_path)
    assert stats == IngestStats(files=0, chunks_seen=0, chunks_upserted=0)
    assert conn.calls == []
    assert conn.commits == 1


# ----------------------------------------------------------------- retriever


def test_sql_targets_runbook_chunks_schema() -> None:
    assert "FROM runbook_chunks" in VECTOR_SQL
    assert "embedding <=> %s::vector" in VECTOR_SQL
    assert "tsv @@ plainto_tsquery('english'" in FTS_SQL
    assert "ORDER BY ts_rank" in FTS_SQL
    assert "FROM runbook_chunks" in HYDRATE_SQL
    assert "chunk_id = ANY(%s)" in HYDRATE_SQL


def test_retrieve_fuses_channels_in_rrf_order() -> None:
    conn = FakeConn(
        results=[
            [("rb:a:one:01",), ("rb:a:two:02",)],  # vector channel
            [("rb:b:three:03",), ("rb:a:one:01",)],  # fts channel
            [  # hydrate
                ("rb:a:one:01", "runbooks/a.md", "Top > One", "alpha content"),
                ("rb:a:two:02", "runbooks/a.md", "Top > Two", "beta content"),
                ("rb:b:three:03", "runbooks/b.md", "Top > Three", "gamma content"),
            ],
        ]
    )
    retriever = RunbookRetriever(conn, embedder=FakeEmbedder(dim=16))
    chunks = retriever.retrieve("S3 AccessDenied cause")

    assert [c.chunk_id for c in chunks] == ["rb:a:one:01", "rb:b:three:03", "rb:a:two:02"]
    assert chunks[0].score == pytest.approx(1 / 60 + 1 / 61)
    assert chunks[1].score == pytest.approx(1 / 60)
    assert chunks[2].score == pytest.approx(1 / 61)
    assert chunks[0].doc_id == "runbooks/a.md"
    assert chunks[0].heading_path == "Top > One"
    assert chunks[0].content == "alpha content"

    sqls = [sql for sql, _ in conn.calls]
    assert len(sqls) == 3
    assert sqls[0] == VECTOR_SQL
    assert sqls[1] == FTS_SQL
    assert sqls[2] == HYDRATE_SQL


def test_retrieve_caps_top_k() -> None:
    conn = FakeConn(
        results=[
            [("rb:a:one:01",), ("rb:a:two:02",)],
            [("rb:b:three:03",), ("rb:a:one:01",)],
            [("rb:a:one:01", "runbooks/a.md", "Top > One", "alpha content")],
        ]
    )
    retriever = RunbookRetriever(conn, embedder=FakeEmbedder(dim=16), top_k=1)
    chunks = retriever.retrieve("S3 AccessDenied cause")
    assert [c.chunk_id for c in chunks] == ["rb:a:one:01"]


def test_retrieve_citations_returns_contract_citations() -> None:
    conn = FakeConn(
        results=[
            [("rb:a:one:01",)],
            [("rb:a:one:01",)],
            [("rb:a:one:01", "runbooks/a.md", "Top > One", "alpha content")],
        ]
    )
    retriever = RunbookRetriever(conn, embedder=FakeEmbedder(dim=16))
    cits = retriever.retrieve_citations("S3 AccessDenied cause")
    assert len(cits) == 1
    c = cits[0]
    assert isinstance(c, ContractCitation)
    assert c.chunk_id == "rb:a:one:01"
    assert c.source == "runbooks/a.md"
    assert c.section == "Top > One"
    assert c.excerpt == "alpha content"


def test_retrieve_empty_channels_returns_nothing() -> None:
    conn = FakeConn(results=[[], []])
    retriever = RunbookRetriever(conn, embedder=FakeEmbedder(dim=16))
    assert retriever.retrieve("nothing matches") == []


# -------------------------------------------------------------------- rerank


def _three_candidates() -> list[RetrievedChunk]:
    return [
        mkchunk("rb:a:1:01", score=0.03),
        mkchunk("rb:a:2:02", score=0.02),
        mkchunk("rb:a:3:03", score=0.01),
    ]


def test_rerank_none_scorer_preserves_rrf_order() -> None:
    out = rerank("query", _three_candidates())
    assert [c.chunk_id for c in out] == ["rb:a:1:01", "rb:a:2:02", "rb:a:3:03"]
    assert all(isinstance(c, ScoredChunk) for c in out)
    assert out[0].rerank_score == 0.03


def test_rerank_none_scorer_caps_top_n() -> None:
    out = rerank("query", _three_candidates(), top_n=2)
    assert [c.chunk_id for c in out] == ["rb:a:1:01", "rb:a:2:02"]


def test_rerank_scorer_is_one_batched_call() -> None:
    calls: list[tuple[str, int]] = []

    def scorer(query: str, candidates: list[RetrievedChunk]) -> list[float]:
        calls.append((query, len(candidates)))
        return [0.1, 0.9, 0.5]

    out = rerank("query", _three_candidates(), scorer=scorer)
    assert calls == [("query", 3)]
    assert [c.chunk_id for c in out] == ["rb:a:2:02", "rb:a:3:03", "rb:a:1:01"]
    assert out[0].rerank_score == 0.9


def test_rerank_scorer_length_mismatch_raises() -> None:
    with pytest.raises(ValueError):
        rerank("query", _three_candidates(), scorer=lambda q, c: [0.1, 0.2])


def test_rerank_ties_are_stable() -> None:
    out = rerank("query", _three_candidates(), scorer=lambda q, c: [0.5, 0.9, 0.9])
    assert [c.chunk_id for c in out] == ["rb:a:2:02", "rb:a:3:03", "rb:a:1:01"]


# -------------------------------------------------------------------- config


def test_retrieval_config_maps_settings() -> None:
    cfg = retrieval_config(Settings())
    assert cfg == RetrievalConfig(
        embedding_dim=1024,
        top_k=8,
        candidates_per_channel=20,
        rrf_k=60,
        rerank_top_n=4,
    )
    assert retrieval_config(Settings(embedding_dim=512)).embedding_dim == 512


def test_no_module_level_settings_singleton() -> None:
    import cloud_investigator.retrieval.config as rc

    assert not hasattr(rc, "settings")
