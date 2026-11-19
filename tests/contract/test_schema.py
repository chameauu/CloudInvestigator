"""Contract tests for the SQL migrations — fully offline (no Postgres needed).

Asserts the schema invariants the rest of the stack relies on:
files exist and are ordered; statements are terminated and balanced; every
CREATE is idempotent (IF NOT EXISTS); 001 runs on plain Postgres (no
extensions); pinned types (text[], jsonb, numeric(3,2)) and status enums are
present; 002 declares its pgvector requirement and ships the hybrid-retrieval
structures (vector(1024) + HNSW, generated tsv + GIN).
"""

from __future__ import annotations

import re
from pathlib import Path

from cloud_investigator.store.db import REQUIRES_EXTENSION_RE

MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"

#: A CREATE TABLE/INDEX that is NOT idempotent — must never appear.
NON_IDEMPOTENT_CREATE = re.compile(
    r"CREATE\s+(?:UNIQUE\s+)?(?:TABLE|INDEX)\s+(?!IF\s+NOT\s+EXISTS)", re.IGNORECASE
)


def _files() -> list[Path]:
    files = sorted(MIGRATIONS.glob("*.sql"))
    assert files, f"no migrations found in {MIGRATIONS}"
    return files


def _read(name: str) -> str:
    return (MIGRATIONS / name).read_text(encoding="utf-8")


def test_migrations_exist_and_are_ordered() -> None:
    files = _files()
    names = [f.name for f in files]
    prefixes = [int(name.split("_", 1)[0]) for name in names]
    assert prefixes == sorted(prefixes), f"migrations out of order: {names}"
    assert len(set(prefixes)) == len(prefixes), f"duplicate migration numbers: {names}"
    assert names[:2] == ["001_core.sql", "002_runbooks.sql"]


def test_migration_files_parse() -> None:
    for path in _files():
        text = path.read_text(encoding="utf-8").strip()
        assert text, f"{path.name} is empty"
        assert text.endswith(";"), f"{path.name} must end with a terminated statement"
        assert text.count("(") == text.count(")"), f"{path.name} has unbalanced parentheses"
        without_comments = re.sub(r"--[^\n]*", "", text)
        assert ";;" not in without_comments, f"{path.name} contains an empty statement"


def test_every_create_is_idempotent() -> None:
    for path in _files():
        text = path.read_text(encoding="utf-8")
        offenders = NON_IDEMPOTENT_CREATE.findall(text)
        assert not offenders, f"{path.name} has CREATE without IF NOT EXISTS: {offenders}"


def test_core_migration_runs_on_plain_postgres() -> None:
    # strip comments first: prose may *mention* extensions/pgvector
    text = re.sub(r"--[^\n]*", "", _read("001_core.sql")).upper()
    assert "EXTENSION" not in text, "001 must not require any extension"
    assert "VECTOR" not in text, "001 must run on plain postgres:18-alpine"


def test_core_tables_types_and_enums() -> None:
    text = _read("001_core.sql")

    for table in (
        "services",
        "investigations",
        "evidence",
        "tool_calls",
        "hypotheses",
        "reports",
        "report_sections",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table} (" in text, f"missing table {table}"

    # pinned types from the blueprint
    assert "text[]" in text, "hypotheses.evidence_ids must be text[]"
    assert "jsonb" in text, "jsonb columns expected in the core ledger"
    assert "numeric(3,2)" in text, "confidence is numeric(3,2)"
    assert "bigserial PRIMARY KEY" in text, "tool_calls/reports ids are bigserial"
    assert "timestamptz" in text, "timestamps are timestamptz (UTC, aware)"
    assert "PRIMARY KEY (report_id, section_id)" in text

    # status enums are enforced at the DB level
    for status in ("'running'", "'done'", "'abstained'", "'failed'"):
        assert status in text, f"investigations status enum missing {status}"
    for status in ("'ok'", "'error'", "'skipped'", "'denied'", "'skipped_budget'"):
        assert status in text, f"tool_calls status enum missing {status}"
    for status in ("'confirmed'", "'refuted'", "'unverified'"):
        assert status in text, f"hypotheses status enum missing {status}"

    # ledger children cascade with their investigation
    assert text.count("ON DELETE CASCADE") >= 4, "children must cascade with investigations"


def test_runbooks_migration_declares_and_uses_pgvector() -> None:
    text = _read("002_runbooks.sql")

    marker = REQUIRES_EXTENSION_RE.search(text)
    assert marker is not None, "002 must declare '-- requires-extension: vector'"
    assert marker.group(1) == "vector"
    assert "CREATE EXTENSION IF NOT EXISTS vector" in text

    # hybrid retrieval: pgvector side
    assert "vector(1024)" in text, "embedding column must be vector(1024)"
    assert "USING hnsw" in text, "HNSW index expected on the embedding"
    assert "vector_cosine_ops" in text, "HNSW must use cosine ops"

    # hybrid retrieval: Postgres FTS side (generated column + GIN index)
    assert re.search(
        r"tsv\s+tsvector\s+GENERATED ALWAYS AS\s*"
        r"\(to_tsvector\('english',\s*content\)\)\s*STORED",
        text,
    ), "tsv must be a stored generated column over to_tsvector('english', content)"
    assert "USING gin (tsv)" in text, "GIN index expected on tsv"

    assert "chunk_id" in text and "UNIQUE" in text, "runbook chunks are addressed by chunk_id"
    assert "incidents_known" in text
    assert "uuid PRIMARY KEY" in text, "incidents_known ids are uuid"
