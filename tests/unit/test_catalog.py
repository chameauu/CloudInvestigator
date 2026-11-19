"""Catalog contract tests — a service map is the deterministic source of
"which resources belong to this service", so parsing must fail fast and loudly.
"""

from __future__ import annotations

import pytest

from cloud_investigator.catalog import (
    Catalog,
    CatalogError,
    get_service,
    load_catalog,
    parse_catalog,
)

MINIMAL = """
version: 1
environment: dev
services:
  s3:
    display_name: "QA data-lake ingestion"
    sources:
      cloudwatch_logs:
        log_groups: ["/aws/lambda/qa-ingest-uploader"]
        filter_pattern: "AccessDenied"
      sql:
        databases: ["qa_audit"]
    resources:
      - provider: aws
        region: eu-west-1
        service: s3
        resource_type: bucket
        resource_id: "arn:aws:s3:::qa-data-lake"
        name: qa-data-lake
        tags: {env: qa}
    runbooks: ["s3-access-denied.md"]
"""


def test_repo_catalog_parses_and_covers_the_demo_service():
    catalog = load_catalog("catalog.yaml")
    svc = get_service(catalog, "s3")
    assert svc is not None
    assert svc.display_name
    # The demo scenario probes exactly these two sources.
    assert {"cloudwatch_logs", "sql"} <= set(svc.sources)
    assert svc.sources["cloudwatch_logs"].log_groups == ["/aws/lambda/qa-ingest-uploader"]
    assert svc.sources["sql"].databases == ["qa_audit"]
    names = {r.name for r in svc.resources}
    assert "qa-data-lake" in names
    bucket = next(r for r in svc.resources if r.name == "qa-data-lake")
    assert bucket.provider == "aws"
    assert bucket.resource_type == "bucket"
    assert bucket.tags["env"] == "qa"


def test_parse_minimal_catalog():
    catalog = parse_catalog(__import__("yaml").safe_load(MINIMAL))
    assert isinstance(catalog, Catalog)
    assert catalog.environment == "dev"
    svc = get_service(catalog, "s3")
    assert svc is not None and svc.runbooks == ["s3-access-denied.md"]


def test_get_service_unknown_returns_none():
    catalog = parse_catalog(__import__("yaml").safe_load(MINIMAL))
    assert get_service(catalog, "nope") is None


def test_missing_file_fails_fast(tmp_path):
    with pytest.raises(CatalogError, match="catalog.yaml"):
        load_catalog(tmp_path / "catalog.yaml")


def test_invalid_yaml_fails_fast(tmp_path):
    path = tmp_path / "catalog.yaml"
    path.write_text("services: [unclosed\n")
    with pytest.raises(CatalogError, match="invalid YAML"):
        load_catalog(path)


def test_unknown_source_binding_fails_fast():
    data = {"services": {"s3": {"sources": {"telepathy": {}}}}}
    with pytest.raises(CatalogError, match="telepathy"):
        parse_catalog(data)


def test_non_mapping_root_fails_fast():
    with pytest.raises(CatalogError, match="mapping"):
        parse_catalog(["not", "a", "mapping"])


def test_bad_resources_fail_fast():
    data = {"services": {"s3": {"resources": "arn:aws:s3:::qa-data-lake"}}}
    with pytest.raises(CatalogError):
        parse_catalog(data)
