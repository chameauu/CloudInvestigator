"""Service catalog — the deterministic map from a service name to its resources.

`catalog.yaml` says which log groups, databases, metrics, and resources belong
to a service. Probes are scoped from this file, never from LLM output: the
agent may choose a service, but the set of things it is allowed to query is
fixed data.

Failure policy: a missing, malformed, or unknown-source catalog is a
configuration error — `CatalogError`, raised at load time, never defaulted.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, ValidationError

from .contracts import ResourceRef, SourceKind

#: Source names a service may bind. Kept explicit so a typo in `catalog.yaml`
#: fails at parse time instead of silently disabling a probe.
KNOWN_SOURCES: frozenset[str] = frozenset(
    {
        "cloudwatch_logs",
        "cloudwatch_metrics",
        "resource_state",
        "sql",
        "runbook",
        "catalog",
        "web",
    }
)


class CatalogError(ValueError):
    """Raised when the catalog cannot be loaded or validated."""


class SourceBinding(BaseModel):
    """Probe scope for one source: exactly what the probe may query."""

    log_groups: list[str] = Field(default_factory=list)
    filter_pattern: str = ""
    databases: list[str] = Field(default_factory=list)
    metrics: list[str] = Field(default_factory=list)
    regions: list[str] = Field(default_factory=list)


class CatalogService(BaseModel):
    """One service: display metadata, source bindings, resources, runbooks."""

    display_name: str = ""
    description: str = ""
    sources: dict[str, SourceBinding] = Field(default_factory=dict)
    resources: list[ResourceRef] = Field(default_factory=list)
    runbooks: list[str] = Field(default_factory=list)


class Catalog(BaseModel):
    version: int = 1
    environment: str = "dev"
    services: dict[str, CatalogService] = Field(default_factory=dict)


def _validate_sources(service: str, payload: dict[str, Any]) -> None:
    if not isinstance(payload, dict):
        raise CatalogError(f"service {service!r}: 'sources' must be a mapping")
    for name in payload:
        if name not in KNOWN_SOURCES:
            raise CatalogError(
                f"service {service!r}: unknown source {name!r} "
                f"(known: {', '.join(sorted(KNOWN_SOURCES))})"
            )


def parse_catalog(data: Any) -> Catalog:
    """Build a `Catalog` from parsed YAML data, validating every level."""
    if not isinstance(data, dict):
        raise CatalogError("catalog root must be a mapping with a 'services' key")
    services = data.get("services")
    if not isinstance(services, dict) or not services:
        raise CatalogError("catalog must define at least one service under 'services'")
    for name, payload in services.items():
        if isinstance(payload, dict):
            _validate_sources(name, payload.get("sources") or {})
        else:
            raise CatalogError(f"service {name!r} must be a mapping")
    try:
        return Catalog.model_validate(data)
    except ValidationError as exc:
        raise CatalogError(f"invalid catalog structure: {exc}") from exc


def load_catalog(path: str | Path) -> Catalog:
    """Load and validate `path`. Raises `CatalogError` — never a silent default."""
    catalog_path = Path(path)
    try:
        text = catalog_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CatalogError(f"cannot read catalog {catalog_path}: {exc}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise CatalogError(f"{catalog_path}: invalid YAML: {exc}") from exc
    return parse_catalog(data)


def get_service(catalog: Catalog, name: str) -> CatalogService | None:
    """Exact service lookup. Unknown service returns None (callers decide)."""
    return catalog.services.get(name)


def match_service(catalog: Catalog, needles: list[str]) -> str:
    """Best-effort service key from free-text hints (deterministic).

    Matches the service key against each hint; falls back to a resource hint
    matched with `ResourceRef.matches`. Returns "" when nothing matches.
    """
    for needle in needles:
        if needle in catalog.services:
            return needle
    for service_name, service in catalog.services.items():
        for hint in needles:
            if any(resource.matches(hint) for resource in service.resources):
                return service_name
    return ""


def source_kind(name: str) -> SourceKind:
    """Narrow a catalog source name to the pinned `SourceKind` literal."""
    if name not in KNOWN_SOURCES:
        raise CatalogError(f"unknown source {name!r}")
    return name  # type: ignore[return-value]  # guarded by KNOWN_SOURCES
