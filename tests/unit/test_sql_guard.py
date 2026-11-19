"""Unit tests: the SQL triple guard (assert_read_sql, standalone and hard) and
the SqlProbe pipeline end-to-end on the committed cassette.

Deterministic: fixtures only, no network, no containers.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from cloud_investigator.config import Settings
from cloud_investigator.contracts import ResourceRef, TimeRange
from cloud_investigator.sources.base import ProbeContext
from cloud_investigator.sources.sql_probe import (
    SqlProbe,
    SqlStatement,
    assert_read_sql,
    has_limit,
)

SCENARIO = "s3_access_denied"
DEMO_WINDOW = TimeRange(
    start=datetime(2026, 11, 18, 8, 0, tzinfo=UTC),
    end=datetime(2026, 11, 18, 12, 0, tzinfo=UTC),
    label="incident window",
)
BUCKET = ResourceRef(
    service="s3",
    resource_type="bucket",
    resource_id="arn:aws:s3:::qa-data-lake",
    name="qa-data-lake",
)

SETTINGS = Settings()  # frozen dev defaults: sql_default_limit=100

EXPECTED_DENY_ROW = (
    "actor=role/qa-ingest-uploader | action=s3:GetObject | decision=DENY | "
    "resource=arn:aws:s3:::qa-data-lake/invoices/2026-11-18/part-0001.csv | "
    "policy_id=ExplicitDenyStmt-bucketpolicy-v3 | ts=2026-11-18T09:14:02Z"
)
EXPECTED_POLICY_ROW = (
    "policy_id=ExplicitDenyStmt-bucketpolicy-v3 | effect=Deny | "
    "principal=role/qa-ingest-uploader | action=s3:GetObject | "
    "resource=arn:aws:s3:::qa-data-lake/* | added_by=role/ops-automation | "
    "applied_at=2026-11-18T09:01:59Z"
)


def make_ctx(**overrides) -> ProbeContext:
    fields: dict = dict(
        investigation_id=f"inv:{SCENARIO}",
        source="sql",
        kind="sql",
        time_range=DEMO_WINDOW,
        resource_targets=[BUCKET],
        scenario=SCENARIO,
        demo_mode=True,
    )
    fields.update(overrides)
    return ProbeContext(**fields)


# --------------------------------------------------------- assert_read_sql


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM tool_calls",
        "DROP TABLE tool_calls",
        "INSERT INTO t VALUES (1)",
        "UPDATE t SET x = 1",
        "CREATE TABLE t (x int)",
        "GRANT ALL ON t TO u",
        "REVOKE ALL ON t FROM u",
        "TRUNCATE t",
        "ALTER TABLE t ADD COLUMN x int",
        "COPY t FROM stdin",
        "CALL do_stuff()",
        "DO $$ BEGIN END $$",
        "MERGE INTO t USING s ON t.id = s.id WHEN MATCHED THEN UPDATE",
        "VACUUM t",
        "ANALYZE t",
        "EXPLAIN SELECT 1",  # read-ish, but the allowlist is SELECT/WITH only
        "TABLE t",
        "SHOW timezone",
    ],
)
def test_write_and_non_select_statements_rejected(sql: str) -> None:
    with pytest.raises(PermissionError):
        assert_read_sql(sql)


@pytest.mark.parametrize(
    "sql",
    [
        # quoted alias — the write word hides inside quotes, still rejected
        'SELECT 1 AS "create"',
        # write word inside a string literal — fail-closed rejects
        "SELECT 'delete' AS note",
        # CTE *named* after a write verb
        "WITH delete AS (SELECT 1) SELECT * FROM delete",
        # standalone 'do' inside a literal — fail-closed rejects
        "SELECT * FROM t WHERE note = 'do not panic'",
    ],
)
def test_write_words_rejected_even_in_quotes_and_literals(sql: str) -> None:
    with pytest.raises(PermissionError):
        assert_read_sql(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1; SELECT 2",
        "SELECT 1;;",
        "SELECT 1; -- done",
        "SELECT ';' AS x",  # fail-closed: ';' inside a literal counts
        "/* ; */ SELECT 1",  # fail-closed: ';' inside a comment counts
        "select 1;drop table x",  # the classic injection attempt
    ],
)
def test_multiple_statements_rejected(sql: str) -> None:
    with pytest.raises(ValueError, match="stray ';'"):
        assert_read_sql(sql)


@pytest.mark.parametrize("sql", ["", "   ", "\n\t", "-- only a comment", "/* x"])
def test_malformed_statements_rejected(sql: str) -> None:
    with pytest.raises(ValueError):
        assert_read_sql(sql)


@pytest.mark.parametrize(
    "sql,expected",
    [
        ("SELECT 1", "SELECT 1 LIMIT 100"),
        ("  select * from t  ", "select * from t LIMIT 100"),
        ("SELECT 1;", "SELECT 1 LIMIT 100"),
        ("-- header\nSELECT 1", "SELECT 1 LIMIT 100"),
        ("/* block */ SELECT 1", "SELECT 1 LIMIT 100"),
        (
            "WITH cte AS (SELECT 1 AS x) SELECT x FROM cte",
            "WITH cte AS (SELECT 1 AS x) SELECT x FROM cte LIMIT 100",
        ),
        (
            # word boundaries are precise: 'deleted' is a name, not DELETE
            "WITH deleted AS (SELECT 1) SELECT * FROM deleted",
            "WITH deleted AS (SELECT 1) SELECT * FROM deleted LIMIT 100",
        ),
        ("SELECT * FROM t LIMIT 5", "SELECT * FROM t LIMIT 5"),
        ("select * from t limit 5", "select * from t limit 5"),
        ("SELECT 1 -- trailing comment", "SELECT 1 -- trailing comment LIMIT 100"),
    ],
)
def test_read_statements_pass_and_get_forced_limit(sql: str, expected: str) -> None:
    assert assert_read_sql(sql, default_limit=100) == expected


def test_default_limit_is_configurable_and_validated() -> None:
    assert assert_read_sql("SELECT 1", default_limit=7) == "SELECT 1 LIMIT 7"
    with pytest.raises(ValueError):
        assert_read_sql("SELECT 1", default_limit=0)


def test_has_limit_helper() -> None:
    assert has_limit("SELECT 1 LIMIT 5")
    assert has_limit("select 1\nlimit 5")
    assert not has_limit("SELECT 1")
    # conservative: a 'limit N' even inside a comment suppresses forcing —
    # appending would corrupt the statement; row caps still bound the result.
    assert has_limit("-- limit 5\nSELECT 1")


# ------------------------------------------------------------- SqlProbe


class _CanaryClient:
    """SqlClient that must never be reached — proves pre-execution rejection."""

    def execute(self, sql, binds=()):  # type: ignore[no-untyped-def]
        raise AssertionError(f"client reached with: {sql}")


class _StaticClient:
    """SqlClient returning fixed rows for any guarded statement."""

    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def execute(self, sql, binds=()):  # type: ignore[no-untyped-def]
        return [dict(row) for row in self._rows]


def test_cassette_sql_probe_replays_the_demo_rows() -> None:
    probe = SqlProbe.cassette(SCENARIO, settings=SETTINGS)
    assert probe.statements[0].ts_column == "ts"

    result = probe.probe(make_ctx())
    assert [tc.status for tc in result.tool_calls] == ["ok", "ok", "error"]
    assert [ev.id for ev in result.evidence] == [
        f"ev:{SCENARIO}:sql:1",
        f"ev:{SCENARIO}:sql:2",
    ]
    deny_row, policy_row = result.evidence
    assert deny_row.excerpt == EXPECTED_DENY_ROW
    assert policy_row.excerpt == EXPECTED_POLICY_ROW
    assert deny_row.ts is not None
    assert deny_row.ts.start == datetime(2026, 11, 18, 9, 14, 2, tzinfo=UTC)
    assert policy_row.ts is not None
    assert policy_row.ts.start == datetime(2026, 11, 18, 9, 1, 59, tzinfo=UTC)
    assert deny_row.raw_ref == f"cassette:{SCENARIO}:sql#1"
    assert deny_row.resource is not None and deny_row.resource.name == "qa-data-lake"

    # the honest failure of the story, recorded verbatim (repr, never reformulated)
    assert len(result.failures) == 1
    assert 'relation "s3_access_logs" does not exist' in result.failures[0]
    envelope = probe.run_statement(probe.statements[2], ctx=make_ctx())
    assert envelope.status == "error"
    assert envelope.error == ("RuntimeError('relation \"s3_access_logs\" does not exist')")


def test_cassette_replay_miss_is_error_not_silence() -> None:
    probe = SqlProbe.cassette(SCENARIO, settings=SETTINGS)
    envelope = probe.run_statement(SqlStatement("SELECT nope FROM nowhere LIMIT 1"), ctx=make_ctx())
    assert envelope.status == "error"
    assert "cassette replay miss" in envelope.error


def test_write_statement_denied_before_execution() -> None:
    probe = SqlProbe(
        _CanaryClient(), statements=[SqlStatement("DELETE FROM tool_calls")], settings=SETTINGS
    )
    envelope = probe.run_statement(probe.statements[0], ctx=make_ctx())
    assert envelope.status == "denied"
    assert "write verb 'DELETE'" in envelope.error
    assert envelope.items == [] and envelope.rows_returned == 0

    record = envelope.to_tool_call_record("sql")
    assert record.status == "skipped"
    assert "write verb 'DELETE'" in record.error

    result = probe.probe(make_ctx())
    assert result.evidence == []
    assert [tc.status for tc in result.tool_calls] == ["skipped"]
    assert result.skipped_reason  # honest skip, never a silent drop


def test_base_substring_guard_still_applies_after_the_sql_guard() -> None:
    # Dual control: this passes the precise SQL guard, but base.py's blunt
    # substring allowlist denies the operation inside the seam — by design.
    probe = SqlProbe(
        _StaticClient([]),
        statements=[SqlStatement("SELECT start_time FROM jobs LIMIT 5")],
        settings=SETTINGS,
    )
    envelope = probe.run_statement(probe.statements[0], ctx=make_ctx())
    assert envelope.status == "denied"
    assert "read-only guard" in envelope.error


def test_limit_forced_into_scope_when_absent() -> None:
    probe = SqlProbe(_StaticClient([{"ok": 1}]), settings=SETTINGS)
    envelope = probe.run_statement(SqlStatement("SELECT 1 as ok"), ctx=make_ctx())
    assert envelope.status == "ok"
    assert envelope.scope["limit_forced"] is True
    assert envelope.scope["sql"] == "SELECT 1 as ok LIMIT 100"
    assert envelope.operation == "query SELECT 1 as ok"  # op echoes the request


def test_rows_normalized_to_flat_items() -> None:
    probe = SqlProbe(
        _StaticClient(
            [
                {
                    "status": "ok",
                    "n": 3,
                    "nothing": None,
                    "at": datetime(2026, 11, 18, 9, 0, tzinfo=UTC),
                }
            ]
        ),
        statements=[SqlStatement("SELECT status, n, nothing, at FROM t LIMIT 5", ts_column="at")],
        settings=SETTINGS,
    )
    envelope = probe.run_statement(probe.statements[0], ctx=make_ctx())
    assert envelope.status == "ok"
    item = envelope.items[0]
    assert item.value == "status=ok | n=3 | nothing=null | at=2026-11-18T09:00:00Z"
    assert item.ts == "2026-11-18T09:00:00Z"
    assert item.resource == "db"  # no database label configured
    assert item.key == "row:1"


def test_budget_stops_statements_after_the_row_cap() -> None:
    bulk = [{"id": position, "ts": "2026-11-18T09:00:00Z"} for position in range(45)]
    probe = SqlProbe(
        _StaticClient(bulk),
        statements=[
            SqlStatement("SELECT id FROM big LIMIT 200", ts_column="ts"),
            SqlStatement("SELECT id FROM big LIMIT 200", ts_column="ts"),
        ],
        settings=SETTINGS,
        max_rows=40,
    )
    result = probe.probe(make_ctx())
    assert [tc.status for tc in result.tool_calls] == ["ok", "skipped"]
    assert len(result.evidence) == 40
    assert result.evidence[-1].truncated is True  # explicit cut marker
    assert result.evidence[0].truncated is False

    envelope = probe.run_statement(probe.statements[1], ctx=make_ctx(), budget_left=0)
    assert envelope.status == "skipped_budget"
