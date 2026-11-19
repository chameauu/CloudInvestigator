# CloudInvestigator — command contract (AGENTS.md §13).
# Stack targets drive the local compose Postgres + pgvector; test targets are
# offline (unit + contract) vs. stack-dependent (integration).

.PHONY: install up down reset seed run serve test test-integration verify

install:
	uv sync

# Start the local stack (Postgres 18 + pgvector via compose) and wait until
# DATABASE_URL accepts connections. Requires Docker.
up:
	docker compose up -d --build
	uv run python scripts/wait_for_db.py

# Stop the stack, keep the data volume.
down:
	docker compose stop

# Destructive: stop the stack and delete the data volume (all local data lost).
reset:
	docker compose down -v

# Refresh demo fixtures. scripts/seed.py lands later in Phase 1; until then
# this fails with a clear message instead of a confusing Python error.
seed:
	@if [ -f scripts/seed.py ]; then \
		uv run python scripts/seed.py; \
	else \
		echo "scripts/seed.py does not exist yet (later Phase 1 lane) — nothing to seed."; \
		exit 1; \
	fi

# End-to-end pipeline, fully offline → one typed InvestigationReport.
run:
	uv run python scripts/smoke.py

# Local API demo: POST /api/investigate + SSE event stream.
serve:
	uv run uvicorn cloud_investigator.api.main:app --reload

# Offline tests only: unit + contract. No network, no containers, DB up or down.
test:
	uv run pytest tests/unit tests/contract -q

# Requires the running stack — run `make up` first, otherwise this fails
# loudly (by design: no silent skips).
test-integration:
	uv run pytest tests/integration -m integration -q

# Full local gate: lint, offline tests, end-to-end smoke.
verify:
	uv run ruff check src scripts tests
	make test
	uv run python scripts/smoke.py
