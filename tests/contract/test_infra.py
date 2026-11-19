"""Contract tests for the local stack definition — offline, files only.

Invariants asserted (AGENTS.md §13 + config.py):
- docker-compose.yml defines the `db` service exactly as DEFAULT_DATABASE_URL
  expects (user/password/db/port), with a healthcheck and a named volume;
- .env.example documents exactly the variables load_settings reads, with the
  dev defaults;
- docker/pgvector.Dockerfile pins postgres:18-alpine + pgvector v0.8.7 and
  purges its build deps;
- the Makefile implements every §13 target;
- pyproject registers the `integration` marker used by make test-integration.

PyYAML is used for the compose file (already a transitive dependency via
langchain-core); tomllib is stdlib. No Docker, no network, no containers.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import yaml

from cloud_investigator import config

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
DOCKERFILE = REPO_ROOT / "docker" / "pgvector.Dockerfile"
ENV_EXAMPLE = REPO_ROOT / ".env.example"
MAKEFILE = REPO_ROOT / "Makefile"
PYPROJECT = REPO_ROOT / "pyproject.toml"

EXPECTED_ENV_VARS = {
    "DATABASE_URL",
    "DEMO_MODE",
    "ENVIRONMENT",
    "RUNBOOK_DIR",
    "MAX_EVIDENCE_PER_PROBE",
    "SQL_STATEMENT_TIMEOUT_MS",
    "SQL_DEFAULT_LIMIT",
    "EMBEDDING_DIM",
}


def _compose() -> dict:
    return yaml.safe_load(COMPOSE_FILE.read_text())


def test_compose_defines_the_db_service() -> None:
    compose = _compose()
    assert "db" in compose["services"], "docker-compose.yml must define the db service"
    db = compose["services"]["db"]
    assert db["environment"] == {
        "POSTGRES_USER": "cloudinvestigator",
        "POSTGRES_PASSWORD": "cloudinvestigator",
        "POSTGRES_DB": "cloudinvestigator",
    }
    assert "5432:5432" in db["ports"]
    # PG18 images want the single mount at /var/lib/postgresql (data dir is a
    # versioned subdirectory inside it); the entrypoint rejects a direct
    # /var/lib/postgresql/data mount.
    assert db["volumes"] == ["cloudinvestigator_pgdata:/var/lib/postgresql"]
    assert "cloudinvestigator_pgdata" in compose["volumes"]
    assert db["healthcheck"]["test"] == ["CMD-SHELL", "pg_isready -U cloudinvestigator"]
    assert db["build"]["dockerfile"] == "pgvector.Dockerfile"


def test_compose_credentials_match_config_default_dsn() -> None:
    env = _compose()["services"]["db"]["environment"]
    dsn = (
        f"postgresql://{env['POSTGRES_USER']}:{env['POSTGRES_PASSWORD']}"
        f"@localhost:5432/{env['POSTGRES_DB']}"
    )
    assert dsn == config.DEFAULT_DATABASE_URL, (
        "docker-compose.yml drifted from config.DEFAULT_DATABASE_URL — "
        "the app and the stack would target different databases"
    )


def test_dockerfile_pins_postgres18_and_pgvector() -> None:
    text = DOCKERFILE.read_text()
    assert "FROM postgres:18-alpine" in text
    assert "--branch v0.8.7" in text, "pgvector pin changed without a decision-log entry"
    assert "PG_CONFIG=/usr/local/bin/pg_config" in text
    assert "apk del" in text, "build deps must be purged in the build layer"
    assert "CREATE EXTENSION IF NOT EXISTS vector" in text


def test_env_example_documents_exactly_the_config_vars() -> None:
    parsed: dict[str, str] = {}
    for line in ENV_EXAMPLE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        assert sep, f".env.example line is not KEY=VALUE: {line!r}"
        parsed[key.strip()] = value.strip()

    assert set(parsed) == EXPECTED_ENV_VARS, (
        ".env.example must document exactly the load_settings variables — "
        "config.py accepts no others"
    )
    assert parsed["DATABASE_URL"] == config.DEFAULT_DATABASE_URL
    assert config.load_settings(env=parsed) == config.Settings(), (
        ".env.example values must equal the dev defaults through the real parser"
    )


def test_makefile_implements_the_command_contract() -> None:
    text = MAKEFILE.read_text()
    targets = re.findall(r"^([a-zA-Z0-9_-]+):", text, re.MULTILINE)
    for target in (
        "install",
        "up",
        "down",
        "reset",
        "seed",
        "run",
        "serve",
        "test",
        "test-integration",
        "verify",
    ):
        assert target in targets, f"Makefile lost the §13 target: {target}"

    assert "docker compose up -d --build" in text
    assert "docker compose stop" in text
    assert "docker compose down -v" in text
    assert "scripts/wait_for_db.py" in text
    assert "tests/unit tests/contract" in text, "make test must stay offline"
    assert "tests/integration -m integration" in text
    assert "ruff check src scripts tests" in text
    assert "scripts/smoke.py" in text
    assert "uvicorn cloud_investigator.api.main:app --reload" in text


def test_pyproject_registers_the_integration_marker() -> None:
    pyproject = tomllib.loads(PYPROJECT.read_text())
    markers = pyproject["tool"]["pytest"]["ini_options"]["markers"]
    assert any(marker.startswith("integration:") for marker in markers), (
        "make test-integration filters with -m integration; the marker must be "
        "declared in pyproject"
    )
