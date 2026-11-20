"""Integration tests — Postgres ledger round-trip and upsert idempotency.

REQUIRES a reachable ledger: ``make up`` (compose stack) or a local Postgres
with ``DATABASE_URL`` exported. When the database is unreachable these tests
FAIL with instructions — they never skip silently.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cloud_investigator.catalog import load_catalog
from cloud_investigator.config import load_settings
from cloud_investigator.contracts import InvestigationReport, ResourceRef, TimeRange
from cloud_investigator.engine.graph import DEFAULT_SCENARIO, run_investigation
from cloud_investigator.engine.resolve import resolve_integrations
from cloud_investigator.sources.base import ProbeContext
from cloud_investigator.sources.fake import load_cassette
from cloud_investigator.sources.probes import make_probe, registered_probes
from cloud_investigator.store.db import (
    apply_migrations,
    connect,
    database_reachable,
    default_migrations_dir,
)
from cloud_investigator.store.ledger import (
    delete_investigation,
    ledger_counts,
    load_report,
    persist_investigation,
    persist_probe_branch,
)

pytestmark = pytest.mark.integration

#: Number of jsonb sections `persist_investigation` writes per report.
EXPECTED_SECTIONS = 9


def _require_ledger() -> None:
    if database_reachable():
        return
    pytest.fail(
        "Postgres ledger unreachable — run `make up` (compose stack) or start a local "
        "Postgres and export DATABASE_URL. This suite requires the ledger; "
        "it never skips silently.",
        pytrace=False,
    )


@pytest.fixture(scope="module")
def migrated() -> list[str]:
    """Apply migrations once (idempotent) — also proves they run from clean."""
    _require_ledger()
    with connect() as conn:
        result = apply_migrations(conn, default_migrations_dir())
    return result.applied


@pytest.fixture()
def clean_investigation(migrated) -> str:
    """A pinned investigation id owned by this suite, wiped before each test.

    Engine runs mint run-scoped ids; tests that must address ledger rows pin
    their own id explicitly (and keep it out of the drill's namespace).
    """
    inv_id = f"inv:test:{DEFAULT_SCENARIO}"
    delete_investigation(inv_id)
    return inv_id


# ------------------------------------------------- scenario helpers (cassette)


def _scenario_fixtures() -> tuple[dict, TimeRange, list[ResourceRef]]:
    """Load the committed cassette — the same scenario the engine runs."""
    cassette = load_cassette(DEFAULT_SCENARIO)
    time_range = TimeRange(**cassette["time_range"])
    resources = [ResourceRef(**r) for r in cassette["demo_script"]["resources"]]
    return cassette, time_range, resources


def _probe_branches(
    time_range: TimeRange, resources: list[ResourceRef], inv_id: str
) -> list[tuple[str, object]]:
    """One ProbeResult per resolved source — exactly what probe_source emits."""
    settings = load_settings()
    catalog_path = Path(settings.catalog_path)
    if not catalog_path.is_absolute() and not catalog_path.exists():
        catalog_path = Path(__file__).resolve().parents[2] / settings.catalog_path
    resolution = resolve_integrations(
        service="s3",
        planned_sources=["cloudwatch_logs", "sql"],
        catalog=load_catalog(catalog_path),
        available_sources=registered_probes(),
    )
    branches = []
    for resolved in resolution.available:
        ctx = ProbeContext(
            investigation_id=inv_id,
            source=resolved.name,
            kind=resolved.kind,
            time_range=time_range,
            resource_targets=resolved.resources or resources,
            params=resolved.params,
            scenario=DEFAULT_SCENARIO,
            demo_mode=True,
        )
        branches.append((resolved.name, make_probe(ctx).probe(ctx)))
    return branches


def _persist_branches(inv_id: str, branches, time_range: TimeRange) -> None:
    scope = {"time_range": time_range.model_dump(mode="json")}
    for _source, result in branches:
        persist_probe_branch(
            inv_id,
            result.evidence,
            [tc.model_dump() for tc in result.tool_calls],
            scope=scope,
        )


# -------------------------------------------------------------------- tests


def test_full_report_round_trip(clean_investigation: str) -> None:
    """migrate → persist a full report (branch write-through + report) →
    load_report → field-by-field equality with the engine's report."""
    inv_id = clean_investigation
    cassette, time_range, resources = _scenario_fixtures()
    assert cassette["scenario_id"] == DEFAULT_SCENARIO

    # 1. per-branch write-through, exactly the payload probe_source produces
    _persist_branches(inv_id, _probe_branches(time_range, resources, inv_id), time_range)

    # 2. the finished report for the same scenario, persisted like the engine does
    report = run_investigation(scenario=DEFAULT_SCENARIO, investigation_id=inv_id)
    persist_investigation(report, report.coverage.model_dump())

    # 3. round trip
    loaded = load_report(inv_id)

    # field-by-field equality across every contract field
    for field in InvestigationReport.model_fields:
        expected = getattr(report, field)
        actual = getattr(loaded, field)
        assert actual == expected, f"field {field!r} mismatch after round-trip"

    assert loaded == report  # full typed-model equality, belt and braces


def test_persist_twice_keeps_row_counts(clean_investigation: str) -> None:
    """Idempotency: persisting the same run twice must not duplicate rows."""
    inv_id = clean_investigation
    _cassette, time_range, resources = _scenario_fixtures()
    branches = _probe_branches(time_range, resources, inv_id)
    report = run_investigation(scenario=DEFAULT_SCENARIO, investigation_id=inv_id)
    coverage = report.coverage.model_dump()

    for _ in range(2):
        _persist_branches(inv_id, branches, time_range)
        persist_investigation(report, coverage)

    counts = ledger_counts(inv_id)
    assert counts["evidence"] == len(report.evidence), counts
    assert counts["tool_calls"] == report.coverage.tool_calls, counts
    assert counts["hypotheses"] == len(report.hypotheses), counts
    assert counts["reports"] == 1, counts
    assert counts["report_sections"] == EXPECTED_SECTIONS, counts

    statuses = dict(counts["hypothesis_statuses"])
    assert statuses == {h.rank: h.status for h in report.hypotheses}, (
        "hypothesis statuses must match the report"
    )


def test_engine_write_through_persists_the_ledger(clean_investigation: str) -> None:
    """run_investigation(persist=True) alone produces the same ledger + report."""
    report = run_investigation(
        scenario=DEFAULT_SCENARIO, persist=True, investigation_id=clean_investigation
    )

    counts = ledger_counts(report.investigation_id)
    assert counts["evidence"] == len(report.evidence), counts
    assert counts["tool_calls"] == report.coverage.tool_calls, counts
    assert counts["hypotheses"] == len(report.hypotheses), counts

    loaded = load_report(report.investigation_id)
    for field in InvestigationReport.model_fields:
        assert getattr(loaded, field) == getattr(report, field), f"field {field!r} mismatch"


def test_two_runs_get_distinct_investigation_ids(clean_investigation: str) -> None:
    """Run-scoped ids: re-running a scenario yields two independent ledgers.

    Without this, a second run would overwrite (or worse, merge into) the first
    run's audit trail — one scenario, one row per execution.
    """
    first = run_investigation(scenario=DEFAULT_SCENARIO, persist=True)
    second = run_investigation(scenario=DEFAULT_SCENARIO, persist=True)

    try:
        assert first.investigation_id != second.investigation_id
        for report in (first, second):
            counts = ledger_counts(report.investigation_id)
            assert counts["present"], counts
            assert counts["evidence"] == len(report.evidence), counts
            assert counts["hypotheses"] == len(report.hypotheses), counts
    finally:
        delete_investigation(first.investigation_id)
        delete_investigation(second.investigation_id)


def test_load_report_missing_investigation_raises_keyerror(migrated) -> None:
    with pytest.raises(KeyError):
        load_report("inv:never_persisted")
