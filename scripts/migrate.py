"""Apply SQL migrations to the configured Postgres ledger.

Usage::

    DATABASE_URL=postgresql://user:pass@localhost:5432/db \\
        uv run python scripts/migrate.py

- ``migrations/001_core.sql`` applies on plain Postgres (no extensions).
- ``migrations/002_runbooks.sql`` declares ``-- requires-extension: vector``
  and is applied only when the pgvector extension is available on the server;
  otherwise it is skipped with a clear message — re-run against the compose
  stack (``make up``, pgvector image) once that is up.
- Every migration is idempotent (IF NOT EXISTS); re-running is safe.
"""

from cloud_investigator.config import load_settings
from cloud_investigator.store.db import (
    apply_migrations,
    connect,
    default_migrations_dir,
    redact_dsn,
)


def main() -> int:
    settings = load_settings()
    print(f"migrate: target {redact_dsn(settings.database_url)}")
    print(f"migrate: dir   {default_migrations_dir()}")
    try:
        with connect(settings) as conn:
            result = apply_migrations(conn, default_migrations_dir())
    except Exception as exc:
        print(f"migrate: FAILED — {exc}")
        return 1

    for name in result.applied:
        print(f"migrate: applied {name}")
    for name, reason in result.skipped:
        print(f"migrate: SKIPPED {name} — {reason}")
        print("migrate:   re-run against the pgvector compose stack (make up) to apply it")
    if not result.applied and not result.skipped:
        print("migrate: nothing to do")
    print("migrate: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
