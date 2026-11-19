"""Integration tests against the running compose stack (`make up` first).

The `integration` marker is applied automatically by conftest for anything in
tests/integration/; `db_up` fails loudly (never skips) if the stack is down.
"""

from __future__ import annotations

import pytest


@pytest.mark.integration
def test_postgres_18_accepts_connections(db_up) -> None:
    with db_up() as conn:
        version: str = conn.execute("SELECT version()").fetchone()[0]
    assert version.startswith("PostgreSQL 18"), version


@pytest.mark.integration
def test_pgvector_extension_is_usable(db_up) -> None:
    # Idempotent: fresh volumes get it from the image's initdb script; this
    # covers warm volumes created before that script existed.
    with db_up() as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        row = conn.execute("SELECT '[1,2,3]'::vector").fetchone()
        conn.commit()
    assert row is not None
    assert str(row[0]) == "[1,2,3]", f"vector literal round-trip failed: {row[0]!r}"
