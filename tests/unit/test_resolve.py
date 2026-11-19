"""resolve_integrations contract tests — « détermine les sources utiles ».

The stage is deterministic: catalog + registry decide what is available.
Unavailable sources are reported with a reason, never silently dropped.
"""

from __future__ import annotations

from cloud_investigator.catalog import load_catalog
from cloud_investigator.engine.resolve import (
    IntegrationResolution,
    resolve_integrations,
    resolve_resources,
)

REGISTERED = ("cloudwatch_logs", "sql")


def _resolve(planned, service="s3", hints=(), registered=REGISTERED):
    return resolve_integrations(
        service=service,
        planned_sources=list(planned),
        catalog=load_catalog("catalog.yaml"),
        available_sources=registered,
        resource_hints=list(hints),
    )


def test_demo_service_resolves_both_probes_with_scope():
    res = _resolve(["cloudwatch_logs", "sql"])
    assert isinstance(res, IntegrationResolution)
    assert res.service == "s3"
    assert res.abstain_reason == ""
    names = [s.name for s in res.available]
    assert names == ["cloudwatch_logs", "sql"]
    cw = res.available[0]
    assert cw.params["log_groups"] == ["/aws/lambda/qa-ingest-uploader"]
    assert cw.params["filter_pattern"] == "qa-data-lake"
    sql = res.available[1]
    assert sql.params["databases"] == ["qa_audit"]
    # service resources travel with every available source
    assert {r.name for r in cw.resources} >= {"qa-data-lake", "qa-ingest-uploader"}


def test_unregistered_source_is_unavailable_with_reason():
    res = _resolve(["cloudwatch_logs", "cloudwatch_metrics"])
    assert [s.name for s in res.available] == ["cloudwatch_logs"]
    assert len(res.unavailable) == 1
    unreachable = res.unavailable[0]
    assert unreachable.name == "cloudwatch_metrics"
    assert "not connected" in unreachable.reason
    assert res.abstain_reason == ""


def test_nothing_available_abstains_with_names():
    res = _resolve(["cloudwatch_metrics", "resource_state"])
    assert res.available == []
    assert res.abstain_reason
    assert "cloudwatch_metrics" in res.abstain_reason
    assert "resource_state" in res.abstain_reason


def test_no_planned_sources_abstains():
    res = _resolve([])
    assert res.available == [] and res.unavailable == []
    assert res.abstain_reason == "no sources planned for this problem"


def test_unknown_service_notes_and_probes_unscoped():
    res = _resolve(["sql"], service="does-not-exist")
    assert [s.name for s in res.available] == ["sql"]
    assert res.available[0].params == {}
    assert any("does-not-exist" in n for n in res.notes)


def test_resource_hints_filter_the_resource_set():
    res = _resolve(["cloudwatch_logs"], hints=["qa-data-lake"])
    assert [r.name for r in res.available[0].resources] == ["qa-data-lake"]


def test_unmatched_hint_keeps_all_resources_and_notes_it():
    res = _resolve(["cloudwatch_logs"], hints=["no-such-thing"])
    assert {r.name for r in res.available[0].resources} >= {"qa-data-lake"}
    assert any("no-such-thing" in n for n in res.notes)


def test_planned_sources_are_deduplicated_preserving_order():
    res = _resolve(["sql", "cloudwatch_logs", "sql"])
    assert [s.name for s in res.available] == ["sql", "cloudwatch_logs"]


def test_resolution_is_json_serializable_for_state():
    payload = _resolve(["cloudwatch_logs", "sql"]).model_dump()
    assert payload["available"][0]["name"] == "cloudwatch_logs"
    assert isinstance(payload["notes"], list)


def test_resolve_resources_without_service_returns_empty():
    resources, notes = resolve_resources(None, [])
    assert resources == []
    assert any("catalog" in n for n in notes)
