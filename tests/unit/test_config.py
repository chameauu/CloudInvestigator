"""Unit tests for cloud_investigator.config — defaults + fail-fast parsing.

Deterministic by construction: every test drives load_settings with an
explicit env mapping (or a monkeypatched os.environ). No network, no
containers, no reliance on the developer's shell environment.
"""

from __future__ import annotations

import pytest

from cloud_investigator import config

INT_KEYS = (
    "MAX_EVIDENCE_PER_PROBE",
    "SQL_STATEMENT_TIMEOUT_MS",
    "SQL_DEFAULT_LIMIT",
    "EMBEDDING_DIM",
)
BOOL_KEYS = ("DEMO_MODE",)
ALL_KEYS = (
    "DATABASE_URL",
    *BOOL_KEYS,
    "ENVIRONMENT",
    "RUNBOOK_DIR",
    *INT_KEYS,
)


def test_defaults_match_the_dev_stack() -> None:
    settings = config.load_settings(env={})
    assert settings == config.Settings()
    assert settings.database_url == config.DEFAULT_DATABASE_URL
    assert settings.database_url == (
        "postgresql://cloudinvestigator:cloudinvestigator@localhost:5432/cloudinvestigator"
    )
    assert settings.demo_mode is True
    assert settings.environment == "dev"
    assert settings.runbook_dir == "runbooks"
    assert settings.max_evidence_per_probe == 40
    assert settings.sql_statement_timeout_ms == 5000
    assert settings.sql_default_limit == 100
    assert settings.embedding_dim == 1024


def test_settings_fixture_returns_pure_defaults(settings: config.Settings) -> None:
    """The conftest fixture must stay the deterministic defaults view."""
    assert settings == config.Settings()


def test_explicit_values_are_parsed() -> None:
    env = {
        "DATABASE_URL": "postgresql://u:sekret@db.example:5433/other",
        "DEMO_MODE": "no",
        "ENVIRONMENT": "qa",
        "RUNBOOK_DIR": "docs/runbooks",
        "MAX_EVIDENCE_PER_PROBE": "7",
        "SQL_STATEMENT_TIMEOUT_MS": "250",
        "SQL_DEFAULT_LIMIT": "5",
        "EMBEDDING_DIM": "384",
    }
    settings = config.load_settings(env=env)
    assert settings == config.Settings(
        database_url="postgresql://u:sekret@db.example:5433/other",
        demo_mode=False,
        environment="qa",
        runbook_dir="docs/runbooks",
        max_evidence_per_probe=7,
        sql_statement_timeout_ms=250,
        sql_default_limit=5,
        embedding_dim=384,
    )


def test_missing_keys_fall_back_to_defaults() -> None:
    assert config.load_settings(env={}) == config.load_settings(env={"DEMO_MODE": "true"})


@pytest.mark.parametrize("raw", [""])
def test_blank_values_fall_back_to_defaults(raw: str) -> None:
    env = {key: raw for key in ALL_KEYS}
    assert config.load_settings(env=env) == config.Settings()


@pytest.mark.parametrize("key", (*INT_KEYS, *BOOL_KEYS))
def test_whitespace_only_typed_values_fail_fast(key: str) -> None:
    """config.py is strict: only "" falls back; "  " is invalid, not default."""
    with pytest.raises(config.ConfigError, match=key):
        config.load_settings(env={key: "  "})


@pytest.mark.parametrize(
    "raw",
    ["postgresql://h:5432/db", "postgresql+psycopg://h:5432/db"],
)
def test_postgresql_dsn_schemes_are_accepted(raw: str) -> None:
    assert config.load_settings(env={"DATABASE_URL": raw}).database_url == raw


@pytest.mark.parametrize(
    "raw",
    ["mysql://h/db", "postgres://h/db", "not a dsn", "postgresql", "://hostonly"],
)
def test_non_postgresql_dsn_fails_fast(raw: str) -> None:
    with pytest.raises(config.ConfigError, match="DATABASE_URL"):
        config.load_settings(env={"DATABASE_URL": raw})


@pytest.mark.parametrize("key", INT_KEYS)
@pytest.mark.parametrize("raw", ["abc", "1.5", "0", "-3", " ", "40abc"])
def test_bad_integers_fail_fast(key: str, raw: str) -> None:
    with pytest.raises(config.ConfigError, match=key):
        config.load_settings(env={key: raw})


@pytest.mark.parametrize("key", INT_KEYS)
def test_positive_integers_are_accepted(key: str) -> None:
    settings = config.load_settings(env={key: "1"})
    assert getattr(settings, key.lower()) == 1


@pytest.mark.parametrize("raw", ["1", "true", "True", "YES", " on "])
def test_truthy_spellings(raw: str) -> None:
    assert config.load_settings(env={"DEMO_MODE": raw}).demo_mode is True


@pytest.mark.parametrize("raw", ["0", "false", "FALSE", "No", " OFF "])
def test_falsy_spellings(raw: str) -> None:
    assert config.load_settings(env={"DEMO_MODE": raw}).demo_mode is False


@pytest.mark.parametrize("raw", ["maybe", "2", "y", "onoff", "yesno"])
def test_bad_booleans_fail_fast(raw: str) -> None:
    with pytest.raises(config.ConfigError, match="DEMO_MODE"):
        config.load_settings(env={"DEMO_MODE": raw})


def test_load_settings_reads_os_environ_when_env_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config.os, "environ", {})
    assert config.load_settings() == config.Settings()

    monkeypatch.setattr(config.os, "environ", {"EMBEDDING_DIM": "512", "DEMO_MODE": "off"})
    settings = config.load_settings()
    assert settings.embedding_dim == 512
    assert settings.demo_mode is False
