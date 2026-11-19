"""SQL probe — triple-guarded read-only queries against authorized databases.

HolmesGPT-style triple guard, fail-closed, in ONE pure function
``assert_read_sql`` (unit-tested standalone):

1. Single statement only — a stray ``;`` anywhere except one optional trailing
   rejects, *including semicolons inside literals*: fail-closed beats clever.
2. Must start with SELECT or WITH (after whitespace/strippable comments), and
   ANY write verb — insert|update|delete|drop|alter|create|grant|revoke|
   truncate|copy|call|do|merge|vacuum|analyze — appearing as a word ANYWHERE
   rejects, quoted aliases and literals included.
3. LIMIT is forced (``settings.sql_default_limit``) when absent.

Defense in depth on top (what the plan pins):

- ``base.assert_read_only`` also runs, inside the ``call()`` seam, on the
  ``query …`` operation. It is a blunt *substring* guard: a SELECT over a
  ``created_at`` column is denied there. Fail-closed by design — base.py is
  LAW and the two guards compose (effective policy = strictest).
- Live mode (psycopg3): every connection issues ``SET TRANSACTION READ ONLY``
  and ``SET LOCAL statement_timeout`` BEFORE the guarded statement — the
  server-side backstop that holds even if a client-side guard were bypassed.

Cassette mode replays ``fixtures/probes/sql_<scenario>.json``; an unrecorded
statement raises a replay-miss error, never a silent empty result.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime
from typing import Any, NamedTuple, Protocol

import psycopg

from ..config import Settings, load_settings
from .base import MAX_EVIDENCE_PER_PROBE, ProbeContext, ProbeResult
from .tool_result import (
    Item,
    ProbeResultBuilder,
    RawResult,
    StructuredToolResult,
    call,
    iso_utc,
    load_probe_cassette,
    raw_ref_prefix,
)

# ------------------------------------------------------------------- guards

#: Write verbs rejected as whole words, anywhere in the statement text —
#: comments and quoted regions included (fail-closed).
WRITE_WORDS = (
    "insert",
    "update",
    "delete",
    "drop",
    "alter",
    "create",
    "grant",
    "revoke",
    "truncate",
    "copy",
    "call",
    "do",
    "merge",
    "vacuum",
    "analyze",
)
_WRITE_RE = re.compile(r"\b(" + "|".join(WRITE_WORDS) + r")\b", re.IGNORECASE)
_LIMIT_RE = re.compile(r"\blimit\s+\d+", re.IGNORECASE)
_HEAD_RE = re.compile(r"[A-Za-z_]+")


def has_limit(sql: str) -> bool:
    """True when the statement text carries an explicit ``LIMIT n``.

    Conservative by design: the scan covers the RAW text — a ``limit N``
    hiding in a comment suppresses forcing (appending a second LIMIT, or
    appending into a trailing comment, would corrupt the statement). The
    row cap in the seam and the client fetch cap still bound every result.
    """
    return _LIMIT_RE.search(sql) is not None


def _strip_leading_comments(body: str) -> str:
    """Strip whitespace and leading ``--``/``/* */`` comments, repeatedly."""
    rest = body.lstrip()
    while True:
        if rest.startswith("--"):
            newline = rest.find("\n")
            if newline == -1:
                return ""
            rest = rest[newline + 1 :].lstrip()
            continue
        if rest.startswith("/*"):
            end = rest.find("*/")
            if end == -1:
                raise ValueError("unterminated block comment in SQL statement")
            rest = rest[end + 2 :].lstrip()
            continue
        return rest


def assert_read_sql(sql: str, *, default_limit: int = 100) -> str:
    """Triple-guard ONE SQL statement; return the executable statement.

    Raises ``ValueError`` for malformed input (empty, multi-statement,
    unterminated comment, bad default_limit) and ``PermissionError`` for policy
    violations (bad prefix, write verb). Callers convert either into a
    pre-execution ``denied`` envelope — the statement never reaches a server.

    The returned statement has its single optional trailing ``;`` removed,
    leading comments stripped, and ``LIMIT {default_limit}`` appended when the
    text carries no explicit LIMIT.
    """
    if not isinstance(sql, str) or not sql.strip():
        raise ValueError("empty SQL statement")
    if default_limit <= 0:
        raise ValueError(f"default_limit must be positive, got {default_limit}")

    body = sql.rstrip()
    if body.endswith(";"):
        body = body[:-1].rstrip()
    # Fail-closed: any remaining ';' — including one inside a literal or a
    # comment — is treated as a second statement.
    if ";" in body:
        raise ValueError(
            "multiple SQL statements: stray ';' (fail-closed: ';' inside "
            "literals/comments counts too)"
        )

    executable = _strip_leading_comments(body)
    if not executable.strip():
        raise ValueError("no SQL statement after comments")

    # Verb scan FIRST (it is the more precise diagnosis for the dangerous
    # class) and on the RAW body — comments included. A write word hiding in
    # a comment or a quoted alias still rejects. Word boundaries keep it
    # precise ('deleted_at' is not DELETE), but the bias is fail-closed.
    write = _WRITE_RE.search(body)
    if write is not None:
        raise PermissionError(
            f"write verb {write.group(0).upper()!r} appears in statement (fail-closed)"
        )

    head = _HEAD_RE.match(executable)
    if head is None or head.group(0).lower() not in ("select", "with"):
        raise PermissionError(
            f"statement must start with SELECT or WITH, got: {sql.strip()[:60]!r}"
        )

    # Raw-text LIMIT check: never append a second LIMIT, never append into a
    # trailing comment (both would corrupt the statement). When forcing is
    # suppressed by a comment, the seam row cap and the client fetch cap
    # still bound the result.
    if not has_limit(body):
        executable = f"{executable} LIMIT {int(default_limit)}"
    return executable


# ------------------------------------------------------------------- clients


class SqlStatement(NamedTuple):
    """One guarded statement plus how to read its rows."""

    sql: str
    binds: tuple[Any, ...] = ()
    ts_column: str = "ts"  # row column carrying the event time ("" ts when absent)


class SqlClient(Protocol):
    """Thin seam over the authorized database."""

    def execute(self, sql: str, binds: Sequence[Any] = ()) -> list[dict[str, Any]]: ...


class CassetteSqlClient:
    """Replays ``fixtures/probes/sql_<scenario>.json``.

    ``execute`` matches the recorded statement (whitespace-normalized,
    trailing ``;`` ignored) and returns its rows, or raises the recorded
    upstream error verbatim. Unrecorded statements raise a replay-miss error.
    """

    def __init__(self, scenario: str) -> None:
        self._data = load_probe_cassette("sql", scenario)
        self._entries = {self._key(entry["sql"]): entry for entry in self._data["queries"]}

    @property
    def database(self) -> str:
        return str(self._data.get("database", ""))

    @property
    def statements(self) -> list[SqlStatement]:
        return [
            SqlStatement(
                entry["sql"],
                tuple(entry.get("binds", ())),
                entry.get("ts_column", "ts"),
            )
            for entry in self._data["queries"]
        ]

    def execute(self, sql: str, binds: Sequence[Any] = ()) -> list[dict[str, Any]]:
        entry = self._entries.get(self._key(sql))
        if entry is None:
            raise RuntimeError(
                "cassette replay miss: statement not recorded for scenario "
                f"{self._data.get('scenario')!r}: {sql[:120]}"
            )
        if entry.get("error"):
            raise RuntimeError(entry["error"])
        return [dict(row) for row in entry.get("rows", [])]

    @staticmethod
    def _key(sql: str) -> str:
        return re.sub(r"\s+", " ", sql.strip().rstrip(";").strip())


class PsycopgSqlClient:
    """Live read-only execution (psycopg3, sync): one connection per statement.

    Every connection issues ``SET TRANSACTION READ ONLY`` and ``SET LOCAL
    statement_timeout`` BEFORE the guarded statement. Those two commands plus
    the pre-execution guards are the dual control the plan pins — the
    server-side backstop holds even if a client-side guard were bypassed.
    """

    def __init__(
        self,
        dsn: str,
        *,
        statement_timeout_ms: int = 5000,
        connect_timeout_s: int = 5,
        fetch_cap: int = MAX_EVIDENCE_PER_PROBE,
    ) -> None:
        self._dsn = dsn
        self._statement_timeout_ms = int(statement_timeout_ms)
        self._connect_timeout_s = int(connect_timeout_s)
        self._fetch_cap = max(1, int(fetch_cap))

    def execute(self, sql: str, binds: Sequence[Any] = ()) -> list[dict[str, Any]]:
        with psycopg.connect(self._dsn, connect_timeout=self._connect_timeout_s) as conn:
            try:
                conn.execute("SET TRANSACTION READ ONLY")
                conn.execute(f"SET LOCAL statement_timeout = {self._statement_timeout_ms}")
                cursor = conn.execute(sql, list(binds))
                if cursor is None or cursor.description is None:
                    return []
                columns = [column.name for column in cursor.description]
                # Memory backstop: fetch cap+1 rows, never the whole result set —
                # the extra row is the seam's truncation signal. Bounds hold
                # even for a statement whose LIMIT forcing was suppressed
                # (e.g. a 'limit N' mentioned inside a comment).
                fetched = cursor.fetchmany(self._fetch_cap + 1)
                return [dict(zip(columns, row, strict=True)) for row in fetched]
            finally:
                conn.rollback()  # read-only: end the transaction without committing


# --------------------------------------------------------------------- probe


def _render(value: Any) -> str:
    """Render one cell for the flat row: null → 'null', datetimes → ISO UTC."""
    if value is None:
        return "null"
    if isinstance(value, datetime):
        return iso_utc(value)
    return str(value)


def _rows_to_items(rows: Sequence[dict[str, Any]], *, resource: str, ts_column: str) -> list[Item]:
    """Normalize DB rows into flat items: ts?, resource, key, verbatim value."""
    return [
        Item(
            ts=iso_utc(row.get(ts_column)),
            resource=resource,
            key=f"row:{position}",
            value=" | ".join(f"{column}={_render(value)}" for column, value in row.items()),
        )
        for position, row in enumerate(rows, start=1)
    ]


class SqlProbe:
    """Authorized-database source adapter (read-only, triple-guarded).

    Interface-compatible with the fake source the graph already calls:
    ``probe(ctx: ProbeContext) -> ProbeResult``.
    """

    def __init__(
        self,
        client: SqlClient,
        *,
        statements: Sequence[SqlStatement] = (),
        database: str = "",
        settings: Settings | None = None,
        max_rows: int = MAX_EVIDENCE_PER_PROBE,
    ) -> None:
        self._client = client
        self._statements = list(statements)
        self._database = database
        self._settings = settings or load_settings()
        self._max_rows = max(1, int(max_rows))

    @classmethod
    def cassette(
        cls,
        scenario: str,
        *,
        settings: Settings | None = None,
        max_rows: int = MAX_EVIDENCE_PER_PROBE,
    ) -> SqlProbe:
        """Demo/offline construction: replay the recorded audit-DB reads."""
        client = CassetteSqlClient(scenario)
        return cls(
            client,
            statements=client.statements,
            database=client.database,
            settings=settings,
            max_rows=max_rows,
        )

    @classmethod
    def live(
        cls,
        statements: Sequence[SqlStatement],
        *,
        database: str = "",
        settings: Settings | None = None,
        max_rows: int = MAX_EVIDENCE_PER_PROBE,
    ) -> SqlProbe:
        """Live construction: psycopg3 against ``settings.database_url``."""
        settings = settings or load_settings()
        client = PsycopgSqlClient(
            settings.database_url,
            statement_timeout_ms=settings.sql_statement_timeout_ms,
        )
        return cls(
            client,
            statements=statements,
            database=database,
            settings=settings,
            max_rows=max_rows,
        )

    @property
    def statements(self) -> list[SqlStatement]:
        return list(self._statements)

    # ------------------------------------------------------------ internals

    def run_statement(
        self,
        statement: SqlStatement,
        *,
        ctx: ProbeContext,
        budget_left: int | None = None,
    ) -> StructuredToolResult:
        """One guarded statement end-to-end: SQL guard → seam → envelope."""
        op = f"query {statement.sql}"
        params: dict[str, Any] = {
            "database": self._database,
            "sql": statement.sql,
            "binds": list(statement.binds),
        }
        scope: dict[str, Any] = {"database": self._database}

        # SQL triple guard FIRST — fail-closed, pre-execution: a rejected
        # statement becomes a denied envelope and the client is never touched.
        try:
            guarded = assert_read_sql(statement.sql, default_limit=self._settings.sql_default_limit)
        except (PermissionError, ValueError) as exc:
            scope["sql"] = statement.sql
            return StructuredToolResult.denied(op, str(exc), params=params, scope=scope)

        # scope echoes what will ACTUALLY run (post-guard), params the request.
        scope.update(sql=guarded, limit_forced=not has_limit(statement.sql))

        def execute(_op: str, _params: dict[str, Any]) -> RawResult:
            rows = self._client.execute(guarded, statement.binds)
            return RawResult(
                rows=_rows_to_items(
                    rows,
                    resource=self._database or "db",
                    ts_column=statement.ts_column,
                )
            )

        return call(
            ctx.source,
            op,
            params,
            ctx=ctx,
            execute=execute,
            scope=scope,
            max_rows=self._max_rows,
            budget_left=budget_left,
        )

    # ------------------------------------------------------------- the node

    def probe(self, ctx: ProbeContext) -> ProbeResult:
        """FakeSource-compatible entry point: one ProbeResult, never a raise."""
        builder = ProbeResultBuilder(ctx, raw_ref_prefix=raw_ref_prefix(ctx))
        for statement in self._statements:
            envelope = self.run_statement(
                statement,
                ctx=ctx,
                budget_left=self._max_rows - builder.evidence_count,
            )
            builder.add(envelope)  # query falls back to the guarded scope sql
        return builder.result()
