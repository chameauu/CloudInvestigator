"""Seed the local demo environment: migrations, then the runbook corpus.

Usage (stack up first — ``make up``):

    uv run python scripts/seed.py

Order matters: migrations create ``runbook_chunks`` (002 is gated on the
pgvector extension), then the ingest fills it with chunked, embedded
runbooks. Both steps are idempotent — re-running is safe.
"""

from __future__ import annotations

import sys
from pathlib import Path

from cloud_investigator.config import ConfigError, load_settings
from cloud_investigator.retrieval.embeddings import FakeEmbedder
from cloud_investigator.retrieval.ingest import RunbookIngestor
from cloud_investigator.store.db import (
    apply_migrations,
    connect,
    default_migrations_dir,
    redact_dsn,
)


def main() -> int:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"seed failed: {exc}", file=sys.stderr)
        return 1

    print(f"seed: target {redact_dsn(settings.database_url)}")
    try:
        with connect(settings) as conn:
            result = apply_migrations(conn, default_migrations_dir())
            for name in result.applied:
                print(f"seed: applied {name}")
            for name, reason in result.skipped:
                print(f"seed: SKIPPED {name} — {reason}")

            ingestor = RunbookIngestor(
                conn,
                embedder=FakeEmbedder(dim=settings.embedding_dim),
                env=settings.environment,
            )
            stats = ingestor.ingest_dir(Path(settings.runbook_dir))
    except Exception as exc:
        print(f"seed failed: {exc}", file=sys.stderr)
        return 1

    if stats.files == 0:
        print(f"seed failed: no *.md runbooks in {settings.runbook_dir!r}", file=sys.stderr)
        return 1
    print(
        f"seed: runbooks files={stats.files} chunks={stats.chunks_seen} "
        f"upserts={stats.chunks_upserted}"
    )
    print("seed: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
