"""Runtime configuration — parsed once at startup, fail-fast, dev-safe defaults.

Every component reads settings through `load_settings()`. No component may
invent its own environment variable or silently fall back to a different
database. Phase 1 is offline-first: the defaults target the local compose
Postgres, and nothing here requires a cloud credential.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

DEFAULT_DATABASE_URL = (
    "postgresql://cloudinvestigator:cloudinvestigator@localhost:5432/cloudinvestigator"
)


class ConfigError(ValueError):
    """Raised when configuration is present but invalid — never silently defaulted."""


@dataclass(frozen=True)
class Settings:
    database_url: str = DEFAULT_DATABASE_URL
    demo_mode: bool = True
    environment: str = "dev"
    runbook_dir: str = "runbooks"
    catalog_path: str = "catalog.yaml"
    max_evidence_per_probe: int = 40
    sql_statement_timeout_ms: int = 5000
    sql_default_limit: int = 100
    embedding_dim: int = 1024


def _int(env: Mapping[str, str], key: str, default: int) -> int:
    raw = env.get(key)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be an integer, got {raw!r}") from exc
    if value <= 0:
        raise ConfigError(f"{key} must be positive, got {value}")
    return value


def _bool(env: Mapping[str, str], key: str, default: bool) -> bool:
    raw = env.get(key)
    if raw is None or raw == "":
        return default
    lowered = raw.strip().lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{key} must be a boolean, got {raw!r}")


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Parse configuration once. Invalid values raise `ConfigError`."""
    source = os.environ if env is None else env
    database_url = source.get("DATABASE_URL", DEFAULT_DATABASE_URL) or DEFAULT_DATABASE_URL
    if not database_url.startswith(("postgresql://", "postgresql+psycopg://")):
        raise ConfigError("DATABASE_URL must be a postgresql:// DSN")
    return Settings(
        database_url=database_url,
        demo_mode=_bool(source, "DEMO_MODE", True),
        environment=source.get("ENVIRONMENT", "dev") or "dev",
        runbook_dir=source.get("RUNBOOK_DIR", "runbooks") or "runbooks",
        catalog_path=source.get("CATALOG_PATH", "catalog.yaml") or "catalog.yaml",
        max_evidence_per_probe=_int(source, "MAX_EVIDENCE_PER_PROBE", 40),
        sql_statement_timeout_ms=_int(source, "SQL_STATEMENT_TIMEOUT_MS", 5000),
        sql_default_limit=_int(source, "SQL_DEFAULT_LIMIT", 100),
        embedding_dim=_int(source, "EMBEDDING_DIM", 1024),
    )
