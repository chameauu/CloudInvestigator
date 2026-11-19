"""Ingest the runbook corpus into `runbook_chunks` (idempotent upserts).

Usage (stack up + migrations applied first):

    uv run python scripts/ingest_runbooks.py [--dir runbooks]

Connects via DATABASE_URL from `load_settings()`, chunks every `*.md` file
in the corpus, embeds with the deterministic offline embedder (dimension
from settings.embedding_dim), and upserts into runbook_chunks keyed by
deterministic chunk_id. Prints counts and exits 0 only on success.
"""

from __future__ import annotations

import argparse
import sys

import psycopg

from cloud_investigator.config import ConfigError, load_settings
from cloud_investigator.retrieval.embeddings import FakeEmbedder
from cloud_investigator.retrieval.ingest import RunbookIngestor


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Ingest runbooks/*.md into runbook_chunks (hybrid RAG store)."
    )
    parser.add_argument(
        "--dir",
        default=None,
        help="runbook corpus root (default: settings.runbook_dir, 'runbooks')",
    )
    args = parser.parse_args(argv)

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"ingest failed: {exc}", file=sys.stderr)
        return 1
    runbook_dir = args.dir or settings.runbook_dir

    try:
        conn = psycopg.connect(settings.database_url)
    except psycopg.Error as exc:
        print(f"ingest failed: database unreachable — {exc}", file=sys.stderr)
        return 1

    try:
        ingestor = RunbookIngestor(
            conn,
            embedder=FakeEmbedder(dim=settings.embedding_dim),
            env=settings.environment,
        )
        stats = ingestor.ingest_dir(runbook_dir)
    except psycopg.Error as exc:
        print(f"ingest failed: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()

    if stats.files == 0:
        print(f"ingest failed: no *.md runbooks found in {runbook_dir!r}", file=sys.stderr)
        return 1

    print(f"files={stats.files} chunks={stats.chunks_seen} upserts={stats.chunks_upserted}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
