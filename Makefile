# CloudInvestigator — Phase 0 command contract.
# Phase 1 adds up/down/reset/seed (compose), pytest suites, ruff lint in verify.

.PHONY: install run serve test verify

install:
	uv sync

# End-to-end pipeline, fully offline → one typed InvestigationReport.
run:
	uv run python scripts/smoke.py

# Local API demo: POST /api/investigate + SSE event stream.
serve:
	uv run uvicorn cloud_investigator.api.main:app --reload

# Phase 0: the deterministic smoke IS the test suite; pytest lands in Phase 1.
test:
	uv run python scripts/smoke.py

# Phase 0 verify == smoke pass gate; Phase 1 adds lint + contract checks.
verify:
	uv run python scripts/smoke.py
