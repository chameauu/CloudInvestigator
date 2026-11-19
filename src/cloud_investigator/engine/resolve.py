"""resolve_integrations — the typed first stage (« détermine les sources utiles »).

Deterministic by construction: the service catalog and the probe registry
decide what is available; the LLM never votes on connectivity. Every planned
source ends up either available (with its scope) or unavailable (with a
reason). Silent drops are impossible.

This module is pure — it reads no environment variable, opens no connection.
The graph node feeding it lives in `engine.graph`.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from pydantic import BaseModel, Field

from ..catalog import Catalog, CatalogService, get_service, source_kind
from ..contracts import ResourceRef


class ResolvedSource(BaseModel):
    """A planned source that will actually be probed, with its bound scope."""

    name: str
    kind: str
    resources: list[ResourceRef] = Field(default_factory=list)
    params: dict[str, Any] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)


class UnavailableSource(BaseModel):
    """A planned source that will not be probed — and why."""

    name: str
    reason: str


class IntegrationResolution(BaseModel):
    """Output of the resolve stage. JSON-serializable for the graph state."""

    service: str
    display_name: str = ""
    available: list[ResolvedSource] = Field(default_factory=list)
    unavailable: list[UnavailableSource] = Field(default_factory=list)
    resources: list[ResourceRef] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    @property
    def abstain_reason(self) -> str:
        """Empty when at least one source is usable."""
        if self.available:
            return ""
        if not self.unavailable:
            return "no sources planned for this problem"
        names = ", ".join(u.name for u in self.unavailable)
        return f"none of the planned sources are available here: {names}"


def resolve_resources(
    service: CatalogService | None, hints: Iterable[str]
) -> tuple[list[ResourceRef], list[str]]:
    """Pick the service's resources, narrowed by free-text hints when they match.

    Deterministic: no hints → every resource; hints → the matching subset; a
    hint that matches nothing is recorded, and the full set is kept so the
    investigation is not silently narrowed.
    """
    if service is None:
        return [], ["service not present in the catalog — probing without resource scope"]
    hints_list = [h for h in hints if h]
    if not hints_list or not service.resources:
        return list(service.resources), []
    matched = [r for r in service.resources if any(r.matches(h) for h in hints_list)]
    if matched:
        return matched, []
    notes = [
        "resource hints matched nothing in the catalog ("
        + ", ".join(hints_list)
        + ") — using every resource of the service"
    ]
    return list(service.resources), notes


def resolve_integrations(
    *,
    service: str,
    planned_sources: Iterable[str],
    catalog: Catalog,
    available_sources: Iterable[str],
    resource_hints: Iterable[str] = (),
) -> IntegrationResolution:
    """Decide, per planned source, whether it can be probed and with what scope."""
    registered = set(available_sources)
    planned: list[str] = []
    for name in planned_sources:
        if name and name not in planned:  # dedupe, keep plan order
            planned.append(name)

    catalog_service = get_service(catalog, service) if service else None
    resolution = IntegrationResolution(service=service)
    notes: list[str] = []

    if catalog_service is not None:
        resolution.display_name = catalog_service.display_name
    elif service:
        notes.append(f"service {service!r} not present in the catalog — sources probe unscoped")

    resources, resource_notes = resolve_resources(catalog_service, list(resource_hints))
    notes.extend(resource_notes)
    resolution.resources = resources

    for name in planned:
        if name not in registered:
            resolution.unavailable.append(
                UnavailableSource(
                    name=name,
                    reason="not connected in this environment (no read-only probe registered)",
                )
            )
            continue
        binding = catalog_service.sources.get(name) if catalog_service else None
        params: dict[str, Any] = {}
        source_notes: list[str] = []
        if binding is not None:
            params = binding.model_dump(exclude_defaults=True, exclude_none=True)
        elif catalog_service is not None:
            source_notes.append(
                f"no catalog binding for source {name!r} on service {service!r} — default scope"
            )
        try:
            kind = source_kind(name)
        except Exception:  # noqa: BLE001 - unknown names must not crash the node
            kind = "unknown"
            source_notes.append(f"source {name!r} is not a known source kind")
        resolution.available.append(
            ResolvedSource(
                name=name,
                kind=kind,
                resources=list(resources),
                params=params,
                notes=source_notes,
            )
        )

    if not planned:
        notes.append("planner selected no sources")
    resolution.notes = notes
    return resolution
