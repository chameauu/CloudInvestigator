"""Wait until DATABASE_URL accepts connections — run by `make up` after compose.

Polls with short connect attempts for up to TIMEOUT_S seconds, then exits:
0 when reachable, 1 with a diagnostic otherwise. Uses the same DSN parsing as
the application (cloud_investigator.config) — there is no second DSN source.
"""

from __future__ import annotations

import sys
import time

import psycopg

from cloud_investigator.config import load_settings

TIMEOUT_S = 60.0
ATTEMPT_INTERVAL_S = 2.0
CONNECT_TIMEOUT_S = 3


def _masked(dsn: str) -> str:
    """Mask the password so local logs/notes never carry credentials."""
    if "://" in dsn and "@" in dsn:
        scheme, rest = dsn.split("://", 1)
        creds, hostpart = rest.rsplit("@", 1)
        if ":" in creds:
            return f"{scheme}://{creds.split(':', 1)[0]}:***@{hostpart}"
    return dsn


def main() -> int:
    dsn = load_settings().database_url
    deadline = time.monotonic() + TIMEOUT_S
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with psycopg.connect(dsn, connect_timeout=CONNECT_TIMEOUT_S):
                print(f"db ready: {_masked(dsn)}")
                return 0
        except psycopg.OperationalError as exc:
            last_error = exc
            print(f"waiting for {_masked(dsn)} ...", file=sys.stderr)
            time.sleep(ATTEMPT_INTERVAL_S)
    print(
        f"db NOT reachable after {TIMEOUT_S:.0f}s: {_masked(dsn)}\n"
        f"last error: {last_error}\nStart the stack with `make up`.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
