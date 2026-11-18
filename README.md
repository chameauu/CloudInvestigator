# CloudInvestigator

CloudInvestigator is a read-only agentic cloud investigation assistant for developers,
Cloud Engineers and DevOps. Given a problem described in natural language, it
determines which sources are useful, identifies the resources involved, probes
the authorized cloud services and databases with strictly read-only, scoped
queries, and correlates the results into an evidence-cited report of probable
causes and recommendations — every claim backed by evidence captured verbatim
by the tools, and honest abstention when the evidence is insufficient.

## Quickstart

```bash
uv sync
uv run python scripts/smoke.py
uv run uvicorn cloud_investigator.api.main:app
```

Then open <http://127.0.0.1:8000/>: submit the natural-language problem, watch
the investigation stream node-by-node, and read the final report. The demo
runs fully offline against the deterministic cassette in `fixtures/` (S3
access-denied scenario) — no cloud credentials, no LLM API keys.
