"""Cassette loading — the offline demo scenario fixtures.

``load_cassette`` is the single reader for ``fixtures/cassette_<scenario>.json``
(the pinned scenario script: time range, planned sources, analyst output). The
per-source probe recordings live in ``fixtures/probes/`` and are replayed by
the adapters in ``sources/probes.py``; this module only loads the scenario.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path


def _fixtures_dir() -> Path:
    """Locate fixtures/ — env override, then CWD, then repo layout."""
    override = os.environ.get("CLOUD_INVESTIGATOR_FIXTURES")
    if override:
        return Path(override)
    local = Path.cwd() / "fixtures"
    if local.is_dir():
        return local
    return Path(__file__).resolve().parents[3] / "fixtures"


@lru_cache(maxsize=8)
def load_cassette(scenario: str) -> dict:
    path = _fixtures_dir() / f"cassette_{scenario}.json"
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)
