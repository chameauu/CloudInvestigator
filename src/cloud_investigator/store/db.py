"""Postgres access — the single place a connection is opened (psycopg3, sync).

Rules:
- The DSN comes from ``config.load_settings().database_url`` — no component
  invents its own environment variable or a second connection path.
- Connections are transactional (autocommit=False); callers commit or the
  context manager commits for them on clean exit.
- ``statement_timeout`` is set from settings on every connection so a stuck
  query can never wedge the ledger.
- Secrets are never logged: only ``redact_dsn()`` output may be printed.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import psycopg
from psycopg import Connection
from psycopg import sql as pg_sql

from ..config import Settings, load_settings

#: Marker a migration file may declare in its leading comment:
#: ``-- requires-extension: <name>``. migrate() applies such a file only when
#: the extension is available in this Postgres installation.
REQUIRES_EXTENSION_RE = re.compile(r"^--\s*requires-extension:\s*(\S+)\s*$", re.MULTILINE)


def redact_dsn(dsn: str) -> str:
    """Mask the password (if any) so a DSN is safe to print."""
    return re.sub(r"://([^:/@\s]+):[^@\s]*@", r"://\1:***@", dsn)


@contextmanager
def connect(settings: Settings | None = None) -> Iterator[Connection]:
    """Open one transactional connection from the configured DSN."""
    s = settings or load_settings()
    conn = psycopg.connect(s.database_url, autocommit=False)
    try:
        with conn.cursor() as cur:
            # Utility commands take no bind parameters — inline the (validated
            # int) timeout as a literal instead.
            cur.execute(
                pg_sql.SQL("SET statement_timeout = {}").format(
                    pg_sql.Literal(s.sql_statement_timeout_ms)
                )
            )
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def database_reachable(settings: Settings | None = None) -> bool:
    """True iff the configured ledger answers a trivial query."""
    try:
        with connect(settings) as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
            return cur.fetchone() is not None
    except psycopg.Error:
        return False


def extension_available(conn: Connection, name: str) -> bool:
    """True iff `name` is installable on this server (pg_available_extensions)."""
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_available_extensions WHERE name = %s", (name,))
        return cur.fetchone() is not None


@dataclass
class MigrationResult:
    """Outcome of one ``apply_migrations`` pass (for scripts to report)."""

    applied: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (file, reason)


def default_migrations_dir() -> Path:
    """Repo ``migrations/`` (uv editable installs keep the package in-tree)."""
    return Path(__file__).resolve().parents[3] / "migrations"


def apply_migrations(conn: Connection, migrations_dir: Path) -> MigrationResult:
    """Apply ``migrations/*.sql`` in filename order, one transaction per file.

    - Each file must be idempotent (IF NOT EXISTS) — re-running is safe.
    - A file declaring ``-- requires-extension: <name>`` is applied only when
      that extension is available; it is skipped (with a reason) otherwise,
      so 001_core.sql works on plain Postgres while 002_runbooks.sql waits
      for a pgvector-capable image.
    """
    files = sorted(migrations_dir.glob("*.sql"))
    if not files:
        raise FileNotFoundError(f"no migrations found in {migrations_dir}")
    result = MigrationResult()
    for path in files:
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            raise ValueError(f"migration file is empty: {path.name}")
        marker = REQUIRES_EXTENSION_RE.search(text)
        if marker and not extension_available(conn, marker.group(1)):
            result.skipped.append(
                (
                    path.name,
                    f"extension {marker.group(1)!r} is not available on this server",
                )
            )
            continue
        try:
            with conn.transaction():
                conn.execute(text)
        except psycopg.Error as exc:
            raise RuntimeError(f"migration {path.name} failed: {exc}") from exc
        result.applied.append(path.name)
    return result
