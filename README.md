# CloudInvestigator

CloudInvestigator is a read-only agentic cloud investigation assistant for developers,
Cloud Engineers and DevOps. Given a problem described in natural language, it
determines which sources are useful, identifies the resources involved, probes
the authorized cloud services and databases with strictly read-only, scoped
queries, and correlates the results into an evidence-cited report of probable
causes and recommendations — every claim backed by evidence captured verbatim
by the tools, and honest abstention when the evidence is insufficient.

## Quickstart (fully offline demo)

```bash
uv sync
make run          # one typed InvestigationReport — no credentials, no API keys
make serve        # browser demo: POST /api/investigate + SSE progress
```

Then open <http://127.0.0.1:8000/>: submit the natural-language problem, watch
the investigation stream node-by-node, and read the final report. The demo
replays deterministic cassettes in `fixtures/` (S3 access-denied scenario).

Each run resolves the service against `catalog.yaml` to decide which resources
are in scope and where to look, probes the sources that are actually
registered, then ranks at least two competing explanations — showing each one
confirmed or eliminated as the evidence arrives. Recommendations carry the
culprit version when the evidence names one, an explicit rollback path, and
the steps that prove recovery.

## Local stack

```bash
make up               # Postgres 18 + pgvector (Docker); waits until ready
make seed             # apply migrations + ingest the runbook corpus
make test             # offline unit + contract tests
make test-integration # integration tests against the running stack
make verify           # lint + offline tests + end-to-end smoke
make down             # stop, keep data
make reset            # stop and delete local state
```

## What it guarantees

- **Read-only by construction** — probe allowlist, a fail-closed SQL
  triple-guard (single statement, SELECT/WITH only, no write verbs, forced
  LIMIT), and `SET TRANSACTION READ ONLY` on every live query.
- **Evidence identity is minted only by tools** — any finding whose citations
  do not resolve to evidence ids in state is demoted, never shown as confirmed.
- **Failures are data** — verbatim upstream errors are recorded and surfaced;
  no data is distinct from an error.
- **Abstention is a correct answer** — insufficient evidence produces an
  explicit abstention, not an invented cause.
- **Investigations are durable** — every run is written to the Postgres ledger
  and checkpointed, so an interrupted investigation resumes without diverging
  from an uninterrupted one.
