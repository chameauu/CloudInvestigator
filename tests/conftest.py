"""Shared pytest plumbing for the unit / contract / integration suites.

The `integration` marker is declared in pyproject `[tool.pytest.ini_options]`
markers. Two rules live here:

- Every test collected under tests/integration/ is auto-marked `integration`,
  so `pytest -m integration` (make test-integration) can never silently
  deselect a test because someone forgot the decorator.
- The `db_up` session fixture FAILS — never skips — when DATABASE_URL is
  unreachable, with a message that says how to start the stack. Silent skips
  are forbidden (AGENTS.md §9).

`settings` (function scope) wraps load_settings over a clean, empty
environment: pure defaults, deterministic for unit tests.
`db_up` resolves the DSN via the real environment (like the application does),
so integration tests honor a DATABASE_URL override.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

import psycopg
import pytest

from cloud_investigator import config

REPO_ROOT = Path(__file__).resolve().parent.parent
INTEGRATION_DIR = REPO_ROOT / "tests" / "integration"

# Short: `make up` already waits for the stack; this is a gate, not a waiter.
DB_CONNECT_TIMEOUT_S = 3
DB_WAIT_S = 10.0


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Auto-apply the pyproject-declared `integration` marker to everything
    under tests/integration/ (the marker helper for that suite)."""
    for item in items:
        if INTEGRATION_DIR in item.path.resolve().parents:
            item.add_marker(pytest.mark.integration)


def _masked(dsn: str) -> str:
    """Mask the password before it can reach test output or notes."""
    if "://" in dsn and "@" in dsn:
        scheme, rest = dsn.split("://", 1)
        creds, hostpart = rest.rsplit("@", 1)
        if ":" in creds:
            return f"{scheme}://{creds.split(':', 1)[0]}:***@{hostpart}"
    return dsn


@pytest.fixture()
def settings() -> config.Settings:
    """load_settings over a clean environment: deterministic dev defaults."""
    return config.load_settings(env={})


@pytest.fixture(scope="session")
def db_up() -> Callable[[], psycopg.Connection]:
    """Session gate for integration tests: prove the stack is reachable.

    Fails loudly (not skips) when DATABASE_URL is unreachable; the message
    points at `make up`. On success returns a connection factory.
    """
    dsn = config.load_settings().database_url
    deadline = time.monotonic() + DB_WAIT_S
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with psycopg.connect(dsn, connect_timeout=DB_CONNECT_TIMEOUT_S):
                break
        except psycopg.OperationalError as exc:
            last_error = exc
            time.sleep(1.0)
    else:
        pytest.fail(
            f"Postgres at {_masked(dsn)} is unreachable ({last_error}).\n"
            "Integration tests need the local compose stack — run `make up` first.",
            pytrace=False,
        )

    def connect() -> psycopg.Connection:
        return psycopg.connect(dsn)

    return connect
