"""CloudWatch Logs probe — cassette-backed now, boto3 behind the same seam later.

Phase 1 replays ``fixtures/probes/cloudwatch_logs_<scenario>.json`` through the
exact seam a live client will use: a thin ``LogsClient`` Protocol with ONE read
method (``filter_log_events``) — no boto3 dependency here. Phase 3 plugs a
boto3 adapter behind that seam; envelope, guards and graph stay untouched.

Guarantees (same as every probe through the ``call()`` seam):

- windowed: the query window is clipped to ``ctx.time_range``; with no context
  window the probe defaults to the last 24h — never unbounded.
- capped: ``limit == max_events``; a full page is conservatively marked
  truncated (the server may be hiding more data behind a token).
- fail-closed: the operation passes ``assert_read_only`` inside the seam;
  upstream (or cassette-replay) failures return error envelopes with the
  verbatim ``repr(exc)`` — never exceptions into the graph.
- evidence ids are minted only here via ``make_evidence_id``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from pydantic import BaseModel

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

#: Probes require an explicit time range with a sensible default (last 24h).
DEFAULT_LOG_WINDOW = timedelta(hours=24)


class LogEvent(BaseModel):
    """One CloudWatch log event as the client seam sees it."""

    ts: datetime
    message: str
    event_id: str = ""
    log_stream: str = ""


class LogsClient(Protocol):
    """Thin read-only seam over the CloudWatch Logs API.

    Phase 3 plugs a boto3 adapter here (``filter_log_events`` maps 1:1 onto
    ``boto3.client("logs").filter_log_events``). Nothing else changes.
    """

    def filter_log_events(
        self,
        log_group: str,
        start: datetime,
        end: datetime,
        filter_pattern: str,
        limit: int,
    ) -> list[LogEvent]: ...


def _utc(moment: datetime) -> datetime:
    """Naive datetimes are read as UTC — comparisons must never explode."""
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


class CassetteLogsClient:
    """Replays ``fixtures/probes/cloudwatch_logs_<scenario>.json``.

    Emulates the read semantics we rely on: events are filtered to the window,
    the filter pattern is a space-separated AND of substring terms, results come
    back in timestamp order, and the caller's limit is honored. A log group
    that is not in the cassette raises a replay-miss error — never a silent
    empty result.
    """

    def __init__(self, scenario: str) -> None:
        self._data = load_probe_cassette("cloudwatch_logs", scenario)
        self._events = [LogEvent(**event) for event in self._data["events"]]

    @property
    def log_group(self) -> str:
        return str(self._data["log_group"])

    @property
    def filter_pattern(self) -> str:
        return str(self._data.get("filter_pattern", ""))

    def filter_log_events(
        self,
        log_group: str,
        start: datetime,
        end: datetime,
        filter_pattern: str,
        limit: int,
    ) -> list[LogEvent]:
        if log_group != self.log_group:
            raise RuntimeError(
                f"cassette replay miss: log group {log_group!r} not recorded "
                f"(recorded: {self.log_group!r})"
            )
        terms = [term for term in filter_pattern.split() if term]
        matched = [
            event
            for event in self._events
            if _utc(start) <= _utc(event.ts) <= _utc(end)
            and all(term in event.message for term in terms)
        ]
        matched.sort(key=lambda event: (event.ts, event.event_id))
        return matched[:limit] if limit > 0 else []


class CloudWatchLogsProbe:
    """CloudWatch Logs source adapter.

    Interface-compatible with the fake source the graph already calls:
    ``probe(ctx: ProbeContext) -> ProbeResult`` — it drops into the
    ``probe_source`` node unchanged.
    """

    def __init__(
        self,
        client: LogsClient,
        *,
        log_group: str = "",
        filter_pattern: str = "",
        max_events: int = MAX_EVIDENCE_PER_PROBE,
        default_window: timedelta = DEFAULT_LOG_WINDOW,
    ) -> None:
        self._client = client
        self.log_group = log_group
        self.filter_pattern = filter_pattern
        self.max_events = max(1, int(max_events))
        self.default_window = default_window

    @classmethod
    def cassette(
        cls, scenario: str, *, max_events: int = MAX_EVIDENCE_PER_PROBE
    ) -> CloudWatchLogsProbe:
        """Demo/offline construction: replay the recorded log stream."""
        client = CassetteLogsClient(scenario)
        return cls(
            client,
            log_group=client.log_group,
            filter_pattern=client.filter_pattern,
            max_events=max_events,
        )

    # ------------------------------------------------------------ internals

    def _window(self, ctx: ProbeContext) -> tuple[datetime, datetime]:
        """Clip to the investigation window; default to the last 24h."""
        if ctx.time_range is not None:
            return _utc(ctx.time_range.start), _utc(ctx.time_range.end)
        now = datetime.now(UTC)
        return now - self.default_window, now

    def collect(self, ctx: ProbeContext) -> StructuredToolResult:
        """One guarded ``filter_log_events`` call, as a typed envelope."""
        start, end = self._window(ctx)
        params: dict[str, Any] = {
            "log_group_name": self.log_group,
            "filter_pattern": self.filter_pattern,
            "limit": self.max_events,
            "start": iso_utc(start),
            "end": iso_utc(end),
        }

        def execute(_op: str, _params: dict[str, Any]) -> RawResult:
            if not self.log_group:
                raise ValueError("no log group configured for this probe")
            if end < start:
                raise ValueError(f"inverted time window: start={iso_utc(start)} end={iso_utc(end)}")
            events = self._client.filter_log_events(
                self.log_group, start, end, self.filter_pattern, self.max_events
            )
            items = [
                Item(
                    ts=iso_utc(event.ts),
                    resource=self.log_group,
                    key=event.event_id or event.log_stream or iso_utc(event.ts),
                    value=event.message,
                )
                for event in events
            ]
            # A full page may hide more data behind a token — mark it, and
            # leave a resumable cursor (Phase 2+ pagination).
            page_full = len(events) >= self.max_events
            next_page = f"after:{iso_utc(events[-1].ts)}" if page_full and events else None
            return RawResult(rows=items, next_page=next_page, truncated=page_full)

        return call(
            ctx.source,
            "filter_log_events",
            params,
            ctx=ctx,
            execute=execute,
            scope=dict(params),  # echo what was actually queried
            max_rows=self.max_events,
        )

    # ------------------------------------------------------------- the node

    def probe(self, ctx: ProbeContext) -> ProbeResult:
        """FakeSource-compatible entry point: one ProbeResult, never a raise."""
        envelope = self.collect(ctx)
        builder = ProbeResultBuilder(ctx, raw_ref_prefix=raw_ref_prefix(ctx))
        builder.add(
            envelope,
            query=(
                f"filter_log_events log_group={self.log_group} "
                f"filter={self.filter_pattern} limit={self.max_events}"
            ),
        )
        return builder.result()
