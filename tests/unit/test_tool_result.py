"""Unit tests: StructuredToolResult envelope, the call() seam, the
ProbeResultBuilder fold — demonstrated end-to-end through the first
cassette-backed probe (CloudWatchLogsProbe).

Deterministic: committed fixtures only, no network, no containers, and no
wall-clock dependence (time is only ever asserted as relative spans/durations).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from cloud_investigator.contracts import ResourceRef, TimeRange
from cloud_investigator.sources.base import EXCERPT_MAX_CHARS, ProbeContext
from cloud_investigator.sources.cloudwatch_logs import (
    CassetteLogsClient,
    CloudWatchLogsProbe,
    LogEvent,
)
from cloud_investigator.sources.tool_result import (
    Item,
    ProbeResultBuilder,
    RawResult,
    StructuredToolResult,
    call,
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


def make_ctx(**overrides) -> ProbeContext:
    fields: dict = dict(
        investigation_id=f"inv:{SCENARIO}",
        source="cloudwatch_logs",
        kind="cloudwatch_logs",
        time_range=DEMO_WINDOW,
        resource_targets=[BUCKET],
        scenario=SCENARIO,
        demo_mode=True,
    )
    fields.update(overrides)
    return ProbeContext(**fields)


def rows(count: int, value: str = "row") -> list[Item]:
    return [
        Item(
            ts=f"2026-11-18T09:00:{position:02d}Z",
            resource="res",
            key=f"row:{position}",
            value=value,
        )
        for position in range(count)
    ]


# --------------------------------------------------------------- the envelope


@pytest.mark.parametrize("status", ["ok", "error", "denied", "skipped_budget", "no_data"])
def test_status_literal_accepts_exactly_the_five_statuses(status: str) -> None:
    assert StructuredToolResult(status=status).status == status


def test_unknown_status_is_rejected_by_the_literal() -> None:
    with pytest.raises(ValidationError):
        StructuredToolResult(status="crashed")


def test_constructors_cover_every_status() -> None:
    ok = StructuredToolResult.ok(
        "filter_log_events",
        params={"a": 1},
        scope={"b": 2},
        items=[Item(value="x")],
        truncated=True,
        next_page="cursor",
        duration_ms=3,
    )
    assert ok.status == "ok"
    assert ok.rows_returned == 1 and ok.truncated and ok.next_page == "cursor"

    error = StructuredToolResult.error("query SELECT 1", "RuntimeError('boom')")
    assert error.status == "error"
    assert error.error == "RuntimeError('boom')" and error.items == []

    denied = StructuredToolResult.denied("delete_thing", "write-like op rejected")
    assert denied.status == "denied" and denied.error == "write-like op rejected"

    skipped = StructuredToolResult.skipped_budget("query SELECT 2", "budget gone")
    assert skipped.status == "skipped_budget" and skipped.error == "budget gone"

    empty = StructuredToolResult.no_data("filter_log_events")
    assert empty.status == "no_data" and empty.error == "" and empty.rows_returned == 0


@pytest.mark.parametrize(
    "envelope_status,record_status",
    [
        ("ok", "ok"),
        ("no_data", "ok"),  # NO_DATA is a successful empty read, not an error
        ("error", "error"),
        ("denied", "skipped"),  # pre-execution policy rejection (AGENTS §7)
        ("skipped_budget", "skipped"),
    ],
)
def test_to_tool_call_record_maps_every_status(envelope_status: str, record_status: str) -> None:
    envelope = StructuredToolResult(
        status=envelope_status,
        operation="op",
        params={"k": "v"},
        duration_ms=7,
        error="reason" if envelope_status != "no_data" else "",
    )
    record = envelope.to_tool_call_record("cloudwatch_logs")
    assert record.source == "cloudwatch_logs"
    assert record.operation == "op" and record.params == {"k": "v"}
    assert record.duration_ms == 7 and record.status == record_status


# ------------------------------------------------------------------ the seam


def test_seam_requires_source_to_match_context() -> None:
    ctx = make_ctx(source="cloudwatch_logs")
    with pytest.raises(ValueError, match="wiring bug"):
        call(
            "sql",
            "filter_log_events",
            {},
            ctx=ctx,
            execute=lambda op, params: RawResult(rows=[]),
        )


@pytest.mark.parametrize(
    "op",
    [
        "delete_bucket",
        "create_log_stream",
        "update_thing",
        "put_object",
        "terminate_instances",
        "run_instances",
        "insert_rows",
        "drop_table",
        "get_all_then_delete_rows",  # read prefix, write verb inside
    ],
)
def test_seam_denies_write_ops_without_raising_or_executing(op: str) -> None:
    seen: list[str] = []
    envelope = call(
        "cloudwatch_logs",
        op,
        {"x": 1},
        ctx=make_ctx(),
        execute=lambda operation, params: (seen.append(operation), RawResult(rows=[]))[1],
        scope={"s": 1},
    )
    assert envelope.status == "denied"
    assert seen == []  # never executed
    assert "read-only guard" in envelope.error or "read allowlist" in envelope.error
    assert envelope.to_tool_call_record("cloudwatch_logs").status == "skipped"


def test_seam_denies_non_allowlisted_read_prefix() -> None:
    envelope = call(
        "cloudwatch_logs",
        "fetch_logs",
        {},
        ctx=make_ctx(),
        execute=lambda op, params: RawResult(rows=[]),
    )
    assert envelope.status == "denied"
    assert "not in read allowlist" in envelope.error


def test_seam_denies_empty_operation() -> None:
    envelope = call(
        "cloudwatch_logs",
        "",
        {},
        ctx=make_ctx(),
        execute=lambda op, params: RawResult(rows=[]),
    )
    assert envelope.status == "denied"
    assert envelope.error == "empty operation"


def test_seam_records_verbatim_upstream_error() -> None:
    boom = RuntimeError('relation "s3_access_logs" does not exist')

    def execute(op: str, params: dict) -> RawResult:
        raise boom

    envelope = call(
        "cloudwatch_logs",
        "filter_log_events",
        {"x": 1},
        ctx=make_ctx(),
        execute=execute,
        scope={"s": 1},
    )
    assert envelope.status == "error"
    assert envelope.error == repr(boom)  # verbatim, never reformulated
    assert envelope.items == [] and envelope.rows_returned == 0
    record = envelope.to_tool_call_record("cloudwatch_logs")
    assert record.status == "error" and record.error == repr(boom)


def test_seam_echoes_scope_verbatim() -> None:
    scope = {"log_group_name": "/aws/lambda/qa-ingest-uploader", "limit": 40}
    envelope = call(
        "cloudwatch_logs",
        "filter_log_events",
        {"limit": 40},
        ctx=make_ctx(),
        execute=lambda op, params: RawResult(rows=rows(2)),
        scope=scope,
    )
    assert envelope.scope == scope


def test_seam_caps_rows_with_truncation_marker() -> None:
    envelope = call(
        "cloudwatch_logs",
        "filter_log_events",
        {},
        ctx=make_ctx(),
        execute=lambda op, params: RawResult(rows=rows(12)),
        max_rows=5,
    )
    assert envelope.status == "ok"
    assert len(envelope.items) == 5 and envelope.rows_returned == 5
    assert envelope.truncated is True


def test_seam_caps_every_item_value() -> None:
    envelope = call(
        "cloudwatch_logs",
        "filter_log_events",
        {},
        ctx=make_ctx(),
        execute=lambda op, params: RawResult(rows=rows(1, value="x" * 5000)),
    )
    value = envelope.items[0].value
    assert value == "x" * EXCERPT_MAX_CHARS + "\n…[truncated 3000 chars]"
    assert envelope.truncated is True


def test_seam_no_data_is_not_error() -> None:
    envelope = call(
        "cloudwatch_logs",
        "filter_log_events",
        {},
        ctx=make_ctx(),
        execute=lambda op, params: RawResult(rows=[]),
    )
    assert envelope.status == "no_data"
    assert envelope.error == "" and envelope.items == []
    record = envelope.to_tool_call_record("cloudwatch_logs")
    assert record.status == "ok" and record.error == ""


def test_seam_budget_exhaustion_skips_before_execution() -> None:
    seen: list[str] = []

    def execute(op: str, params: dict) -> RawResult:
        seen.append(op)
        return RawResult(rows=rows(1))

    envelope = call(
        "cloudwatch_logs",
        "filter_log_events",
        {},
        ctx=make_ctx(),
        execute=execute,
        budget_left=0,
    )
    assert envelope.status == "skipped_budget"
    assert seen == []
    assert "row budget exhausted" in envelope.error


def test_seam_partial_budget_caps_rows_within_the_call() -> None:
    envelope = call(
        "cloudwatch_logs",
        "filter_log_events",
        {},
        ctx=make_ctx(),
        execute=lambda op, params: RawResult(rows=rows(10)),
        max_rows=40,
        budget_left=5,
    )
    assert len(envelope.items) == 5 and envelope.rows_returned == 5
    assert envelope.truncated is True


def test_seam_ok_passes_through_cursor_and_duration() -> None:
    payload = rows(3)
    envelope = call(
        "cloudwatch_logs",
        "filter_log_events",
        {},
        ctx=make_ctx(),
        execute=lambda op, params: RawResult(rows=payload, next_page="token"),
        max_rows=10,
    )
    assert envelope.status == "ok"
    assert envelope.items == payload and envelope.rows_returned == 3
    assert envelope.next_page == "token" and not envelope.truncated
    assert envelope.duration_ms >= 0


# -------------------------------------------------------------------- fold


def test_builder_mints_deterministic_evidence() -> None:
    builder = ProbeResultBuilder(make_ctx(), raw_ref_prefix=f"cassette:{SCENARIO}:cloudwatch_logs")
    builder.add(
        StructuredToolResult.ok(
            "query SELECT x",
            scope={"sql": "SELECT x LIMIT 100"},
            items=[Item(ts="2026-11-18T09:14:02Z", resource="r", key="row:1", value="v")],
        )
    )
    result = builder.result()
    assert [ev.id for ev in result.evidence] == [f"ev:{SCENARIO}:cloudwatch_logs:1"]
    evidence = result.evidence[0]
    assert evidence.query == "SELECT x LIMIT 100"  # scope echo = exact query
    assert evidence.raw_ref == f"cassette:{SCENARIO}:cloudwatch_logs#1"
    assert evidence.kind == "cloudwatch_logs"
    assert evidence.resource is not None and evidence.resource.resource_id.endswith("qa-data-lake")
    assert evidence.ts is not None
    assert evidence.ts.start == evidence.ts.end == datetime(2026, 11, 18, 9, 14, 2, tzinfo=UTC)
    assert evidence.excerpt == "v" and not evidence.truncated
    assert result.tool_calls[0].status == "ok"


def test_builder_error_envelope_becomes_failure_not_evidence() -> None:
    builder = ProbeResultBuilder(make_ctx(), raw_ref_prefix="cassette:x")
    builder.add(StructuredToolResult.error("query SELECT bad", "RuntimeError('boom')"))
    result = builder.result()
    assert result.evidence == []
    assert result.tool_calls[0].status == "error"
    assert result.failures == ["cloudwatch_logs: query SELECT bad → RuntimeError('boom')"]


def test_builder_denied_sets_skipped_reason_when_nothing_yielded() -> None:
    builder = ProbeResultBuilder(make_ctx(), raw_ref_prefix="cassette:x")
    builder.add(StructuredToolResult.denied("delete_thing", "read-only guard says no"))
    result = builder.result()
    assert result.evidence == []
    assert result.tool_calls[0].status == "skipped"
    assert "read-only guard says no" in result.skipped_reason


# ----------------------------------------------- CloudWatchLogsProbe end-to-end


def test_cassette_logs_probe_replays_the_s3_story() -> None:
    probe = CloudWatchLogsProbe.cassette(SCENARIO)
    assert probe.log_group == "/aws/lambda/qa-ingest-uploader"
    assert probe.filter_pattern == "qa-data-lake"

    result = probe.probe(make_ctx())
    assert [tc.status for tc in result.tool_calls] == ["ok"]
    assert result.failures == []

    assert [ev.id for ev in result.evidence] == [
        f"ev:{SCENARIO}:cloudwatch_logs:1",
        f"ev:{SCENARIO}:cloudwatch_logs:2",
    ]
    first, second = result.evidence
    # chronological replay: policy-change event, then the failed upload
    assert first.ts is not None
    assert first.ts.start == datetime(2026, 11, 18, 9, 2, 41, 872000, tzinfo=UTC)
    assert "PutBucketPolicy" in first.excerpt
    assert "AccessDenied" in second.excerpt
    assert second.excerpt.startswith("2026-11-18T09:14:02.114Z ERROR")
    assert all(
        ev.raw_ref.startswith(f"cassette:{SCENARIO}:cloudwatch_logs#") for ev in result.evidence
    )
    assert all(not ev.truncated for ev in result.evidence)
    assert result.evidence[0].query == (
        "filter_log_events log_group=/aws/lambda/qa-ingest-uploader filter=qa-data-lake limit=40"
    )


def test_cassette_logs_probe_no_data_outside_window() -> None:
    probe = CloudWatchLogsProbe.cassette(SCENARIO)
    far_future = TimeRange(
        start=datetime(2030, 1, 1, tzinfo=UTC), end=datetime(2030, 1, 2, tzinfo=UTC)
    )
    ctx = make_ctx(time_range=far_future)
    assert probe.collect(ctx).status == "no_data"
    result = probe.probe(ctx)
    assert result.evidence == []
    assert result.tool_calls[0].status == "ok"  # NO_DATA ≠ ERROR


class _StubClient:
    """Deterministic LogsClient stand-in: fixed events, recorded calls."""

    def __init__(self, events: list[LogEvent]) -> None:
        self.events = events
        self.calls: list[tuple[str, datetime, datetime, str, int]] = []

    def filter_log_events(self, log_group, start, end, filter_pattern, limit):
        self.calls.append((log_group, start, end, filter_pattern, limit))
        return self.events


def test_window_defaults_to_last_24h_never_unbounded() -> None:
    client = _StubClient([])
    probe = CloudWatchLogsProbe(client, log_group="/aws/lambda/x")
    envelope = probe.collect(make_ctx(time_range=None, demo_mode=False))
    assert len(client.calls) == 1
    _group, start, end, _pattern, _limit = client.calls[0]
    assert timedelta(hours=23, minutes=59) <= end - start <= timedelta(hours=24)
    assert end.tzinfo is not None
    assert envelope.status == "no_data"  # stub has nothing; still a clean read


def test_window_clipped_to_ctx_time_range() -> None:
    client = _StubClient([])
    probe = CloudWatchLogsProbe(client, log_group="/aws/lambda/x")
    probe.collect(make_ctx())
    _group, start, end, _pattern, _limit = client.calls[0]
    assert (start, end) == (DEMO_WINDOW.start, DEMO_WINDOW.end)


def test_full_page_is_marked_truncated_with_cursor() -> None:
    events = [
        LogEvent(
            ts=datetime(2026, 11, 18, 9, 0, tzinfo=UTC) + timedelta(seconds=i),
            message=f"m{i}",
            event_id=f"e{i}",
        )
        for i in range(50)
    ]
    probe = CloudWatchLogsProbe(_StubClient(events), log_group="g", max_events=10)
    envelope = probe.collect(make_ctx())
    assert envelope.status == "ok"
    assert len(envelope.items) == 10 and envelope.rows_returned == 10
    assert envelope.truncated is True and envelope.next_page


def test_cassette_replay_miss_is_error_not_silence() -> None:
    probe = CloudWatchLogsProbe(CassetteLogsClient(SCENARIO), log_group="/aws/lambda/nope")
    envelope = probe.collect(make_ctx())
    assert envelope.status == "error"
    assert "cassette replay miss" in envelope.error


def test_inverted_window_is_a_clean_error() -> None:
    probe = CloudWatchLogsProbe(_StubClient([]), log_group="g")
    inverted = TimeRange(
        start=datetime(2026, 11, 18, 12, 0, tzinfo=UTC),
        end=datetime(2026, 11, 18, 8, 0, tzinfo=UTC),
    )
    envelope = probe.collect(make_ctx(time_range=inverted))
    assert envelope.status == "error"
    assert "inverted time window" in envelope.error


def test_missing_log_group_is_a_clean_error() -> None:
    probe = CloudWatchLogsProbe(_StubClient([]), log_group="")
    envelope = probe.collect(make_ctx())
    assert envelope.status == "error"
    assert "no log group" in envelope.error
