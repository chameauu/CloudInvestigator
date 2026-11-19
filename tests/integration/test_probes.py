"""Integration: the SQL probe against the real Postgres stack.

Proves the three pinned guarantees against a live server:

1. a guarded SELECT through ``SqlProbe`` comes back ``ok`` (LIMIT forced);
2. a write attempt (``DELETE FROM tool_calls``) is denied PRE-execution — the
   client is never reached;
3. the server-side backstop: on a raw connection, ``SET TRANSACTION READ
   ONLY`` makes Postgres itself reject a write even if every client-side guard
   were bypassed — the psycopg error is recorded verbatim.

Requires the running stack: the shared ``db_up`` fixture FAILS with a
"run `make up`" message when DATABASE_URL is unreachable — never a silent skip.
"""

from __future__ import annotations

import psycopg
import pytest

from cloud_investigator.config import load_settings
from cloud_investigator.sources.base import ProbeContext
from cloud_investigator.sources.sql_probe import SqlProbe, SqlStatement

pytestmark = pytest.mark.integration

#: Throwaway table for the backstop check (test fixture only — the probe layer
#: itself never writes; see AGENTS §1 "no write path, ever").
_BACKSTOP_TABLE = "ci_integration_backstop"


def _ctx() -> ProbeContext:
    return ProbeContext(
        investigation_id="inv:integration",
        source="sql",
        kind="sql",
        demo_mode=False,
    )


def test_guarded_select_ok(db_up) -> None:
    settings = load_settings()
    probe = SqlProbe.live([SqlStatement("SELECT 1 as ok")], settings=settings)

    envelope = probe.run_statement(probe.statements[0], ctx=_ctx())
    assert envelope.status == "ok"
    assert envelope.rows_returned == 1
    assert envelope.items[0].value == "ok=1"
    assert envelope.scope["limit_forced"] is True
    assert envelope.scope["sql"] == f"SELECT 1 as ok LIMIT {settings.sql_default_limit}"
    assert envelope.error == ""

    result = probe.probe(_ctx())
    assert [tc.status for tc in result.tool_calls] == ["ok"]
    assert result.evidence and result.evidence[0].excerpt == "ok=1"
    assert result.failures == []


def test_write_denied_pre_execution(db_up) -> None:
    probe = SqlProbe.live([SqlStatement("DELETE FROM tool_calls")], settings=load_settings())

    envelope = probe.run_statement(probe.statements[0], ctx=_ctx())
    assert envelope.status == "denied"
    assert "write verb 'DELETE'" in envelope.error
    assert envelope.items == [] and envelope.rows_returned == 0

    record = envelope.to_tool_call_record("sql")
    assert record.status == "skipped" and "write verb 'DELETE'" in record.error

    result = probe.probe(_ctx())
    assert result.evidence == []
    assert [tc.status for tc in result.tool_calls] == ["skipped"]
    assert result.skipped_reason  # honest skip, never a silent drop


def test_server_side_read_only_backstop(db_up) -> None:
    """SET TRANSACTION READ ONLY blocks the write IN THE SERVER, with the
    client-side guards fully bypassed (raw connection, no probe)."""
    with db_up() as setup:  # test fixture setup only
        setup.execute(f"CREATE TABLE IF NOT EXISTS {_BACKSTOP_TABLE} (id integer)")

    verbatim = ""
    try:
        with db_up() as conn:
            conn.execute("SET TRANSACTION READ ONLY")
            with pytest.raises(psycopg.Error) as excinfo:
                conn.execute(f"DELETE FROM {_BACKSTOP_TABLE}")
            verbatim = str(excinfo.value)
            assert getattr(excinfo.value, "sqlstate", None) == "25006"
            conn.rollback()
    finally:
        with db_up() as teardown:
            teardown.execute(f"DROP TABLE IF EXISTS {_BACKSTOP_TABLE}")

    # recorded verbatim — never reformulated
    assert "cannot execute DELETE in a read-only transaction" in verbatim
