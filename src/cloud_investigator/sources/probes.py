"""Probe registry — the graph's single dispatch to read-only sources.

Phase 1 replaces the Phase 0 fake adapters with the real, envelope-guarded
probes (CloudWatch Logs, SQL) while keeping the cassette path for offline
demos. Live cloud adapters plug in behind this same registry in Phase 3:
``make_probe`` is the only constructor the graph calls, so adding a source
never touches the graph.

Demo/cassette mode replays ``fixtures/probes/<source>_<scenario>.json``;
live mode fails loudly until its adapter lands — no silent fake fallback.
"""

from __future__ import annotations

from .base import ProbeContext
from .cloudwatch_logs import CloudWatchLogsProbe
from .sql_probe import SqlProbe

#: SourceKind literal -> probe class. The gate's "connected sources" list.
_PROBES = {
    "cloudwatch_logs": CloudWatchLogsProbe,
    "sql": SqlProbe,
}


def registered_probes() -> tuple[str, ...]:
    """Sources with a real probe connected here (the gate's allowlist)."""
    return tuple(sorted(_PROBES))


def make_probe(ctx: ProbeContext):
    """Build the probe for ``ctx.source`` (cassette demo or live adapter).

    Raises ``KeyError`` for an unregistered source and ``RuntimeError`` for a
    live request with no adapter yet — both are wiring errors, not data.
    """
    probe_cls = _PROBES.get(ctx.source)
    if probe_cls is None:
        raise KeyError(f"no probe registered for source {ctx.source!r}")
    if ctx.demo_mode:
        return probe_cls.cassette(ctx.scenario)
    raise RuntimeError(
        f"live probe for {ctx.source!r} is not wired yet (Phase 3) — "
        "run with demo_mode=True or add the live adapter"
    )
