# CloudInvestigator — System Architecture

**Mission:** from a natural-language problem, run a *strictly read-only* investigation across authorized cloud services and SQL databases, and produce an evidence-cited report of probable causes + recommendations. Abstention when evidence is insufficient is a *correct* answer. No remediation, no write path — ever.

**Stack:** Python 3.13 · FastAPI · LangGraph · RAG · SQL (PostgreSQL 18 + pgvector). Managed with `uv`; offline-first via recorded cassettes.

## 1. Bird's-eye view

```text
┌─────────────────────────────────────────────────────────────────────────┐
│ CLIENTS      web demo page · curl · (later) CLI/eval harness             │
└─────────────────────────────────────────────────────────────────────────┘
   │  POST /api/investigate {question, scenario}        GET …/events (SSE)
┌─────────────────────────────────────────────────────────────────────────┐
│ SERVING      FastAPI (api/main.py)                                       │
│              • sync POST runs the graph once, stores the event log       │
│              • SSE streams node-by-node progress → final report          │
└─────────────────────────────────────────────────────────────────────────┘
   │
┌─────────────────────────────────────────────────────────────────────────┐
│ ORCHESTRATION  LangGraph StateGraph (engine/graph.py)                    │
│                                                                          │
│  intake → plan → gate ──Send()──► probe_source × N (parallel branches)   │
│                                    │  CloudWatch Logs probe              │
│                                    │  CloudWatch Metrics probe           │
│                                    │  Resource-state probe (describe_*)  │
│                                    │  SQL probe (read-only triple-guard) │
│                                    │  Runbook RAG probe (hybrid+cites)   │
│                                    ▼                                     │
│              correlate ◄── merge_list reducers (keyed fan-in)            │
│                  │                                                      │
│              hypothesize (≥2 competing candidates)                       │
│                  │                                                      │
│              verify  (deterministic citation-ID check, reject-and-retry   │
│                      once, then demote to unverified hypothesis)          │
│                  │                                                      │
│              report → typed InvestigationReport                          │
│                                                                          │
│  state: InvestigationState (TypedDict channels; evidence/tool_calls/    │
│         timeline/citations merged via append reducers)                   │
│  durable: LangGraph Postgres checkpointer, thread_id = investigation_id   │
└─────────────────────────────────────────────────────────────────────────┘
   │                                   │
   │  every boundary typed              │  write-through
┌──────────────────────────────┐   ┌──────────────────────────────────────┐
│ CONTRACTS (contracts.py —   │   │ LEDGER  Postgres 18 (+pgvector)       │
│ Pydantic, law at every node) │   │  investigations · evidence ·          │
│  TimeRange · ResourceRef     │   │  tool_calls · hypotheses · reports ·  │
│  EvidenceItem · Citation     │   │  report_sections                      │
│  Finding · Hypothesis        │   │  runbook_chunks (vector 1024 + tsv)   │
│  ProbePlan · ProbeResult     │   │  incidents_known (past-incident mem) │
│  ToolCallRecord · Coverage   │   │  + LangGraph checkpoint tables        │
│  InvestigationReport         │   │  replay via query_hash; audit every   │
│  (abstained, abstain_reason) │   │  tool call in tool_calls              │
└──────────────────────────────┘   └──────────────────────────────────────┘
```

## 2. The seven layers

**L1 — Serving (FastAPI).** `POST /api/investigate` validates the request and runs the graph; `GET /api/investigate/{id}/events` streams node events (SSE) then the report; an inline HTML page demos it. Event shapes are stable — live streaming later swaps the internals without touching clients.

**L2 — Orchestration (LangGraph).** One `StateGraph`, eight node kinds. `plan` produces a `ProbePlan` (which sources, which resources, what time window). `gate` short-circuits on noise (nothing to probe → abstain early). `Send(probe_source, ProbeBranch)` fans out **one branch per planned source** in parallel; each branch writes only evidence / tool_calls / timeline / citations, merged back by `merge_list` reducers (keyed fan-in prevents `Send()` collisions). `correlate` normalizes and dedups evidence; `hypothesize` demands **≥2 competing candidates**; `verify` is pure code; `report` assembles the typed report with an honest `CoverageReport`.

**L3 — Contracts (the law).** Pydantic models at every node boundary — no free-text handoffs. Three rules are enforced structurally: (1) **evidence IDs are minted only by tools** (`ev:{investigation}:{source}:{seq}`), LLM output can only reference them; (2) a causal claim whose citation doesn't resolve is demoted to an unverified Hypothesis, never a Finding; (3) every query is windowed (`TimeRange`, default 24h) and capped.

**L4 — Probe layer (the read-only chokepoint).** One auditable `call()` seam per provider (HolmesGPT-style `StructuredToolResult` envelope). Before any client is created, `assert_read_only()` checks a fail-closed prefix allowlist (`describe/list/get/query/search/filter/head`…) and rejects write verbs — a prompt-injected "delete" becomes `ToolCallRecord(status="skipped")` with a reason, never an exception. Every result: capped excerpts (2 000 chars), default row/log limits, explicit truncation markers, scope echo, **verbatim upstream errors** (`status="error"`), NO_DATA ≠ ERROR. SQL adds the **triple guard**: statement must be `SELECT`/`WITH`, no multi-statement/write verbs, forced `LIMIT`, plus `SET TRANSACTION READ ONLY`, `statement_timeout=5s`, and a dedicated read-only DB role. Sources: `cloudwatch_logs`, `cloudwatch_metrics`, `resource_state`, `sql`, `runbook`, `catalog` (+ optional `web`), all cassette-backed in demo mode.

**L5 — Knowledge/RAG.** Runbooks and past postmortems are chunked (markdown, heading breadcrumbs) into `runbook_chunks`: `content` + `tsvector` (FT) + `vector(1024)` (pgvector, HNSW cosine). Hybrid retrieval = full-text + vector, fused with **Reciprocal Rank Fusion**, reranked; every returned chunk becomes a `Citation{chunk_id, source, section, score, excerpt}` attached to recommendations. Runbook chunks are injected **bounded**; when retrieval is weak, the system says so instead of guessing.

**L6 — Ledger (Postgres).** One canonical store: every investigation, evidence item, tool call, hypothesis, and report section is persisted write-through. `tool_calls` doubles as audit + budget + replay (`query_executed`, `query_hash`). The LangGraph checkpointer persists graph state under `thread_id = investigation_id` → the **restart drill** (interrupt → resume → ledger matches) and the **replay drill** (same cassette twice → identical report, determinism gate).

**L7 — Determinism & evaluation.** Everything authorizing is code (guards, ID checks, policy tables, fallbacks); the LLM only correlates and narrates — code authorizes, the model never executes. `FakeLLM` + cassettes make the whole pipeline reproducible offline; the eval harness scores confirmed-hypothesis accuracy *and* abstention correctness on golden scenarios, including a poisoned-evidence prompt-injection case.

## 3. Security model (dual control)

| Control | Layer 1 | Layer 2 |
|---|---|---|
| Cloud APIs | tool allowlist (fail-closed, pre-client) | read-only IAM role per env (dev/qa) |
| SQL | triple-guard + `SET TRANSACTION READ ONLY` + timeout + forced LIMIT | dedicated read-only DB role |
| LLM | evidence = data, never instructions | poisoned-evidence eval scenario |
| Secrets | `.env.example` placeholders, parse-once-fail-fast | never committed |

Read-only is enforced at **both** the tool layer and the credential layer — one alone leaves a gap. There is no remediation path anywhere in the graph, and no code path that can construct a write operation.

## 4. Storage schema (summary)

| Table | Role |
|---|---|
| `investigations` | one row per run: question, env, status, plan, summary, confidence, timestamps |
| `evidence` | one row = one verifiable fact: source_type, resource_ref, time window, capped excerpt, query + hash |
| `tool_calls` | audit + budget + replay: operation, params, scope echo, status (`ok/error/skipped/denied/skipped_budget`), duration, rows, verbatim error |
| `hypotheses` | ranked candidates with status `confirmed/refuted/unverified`, arguments for/against, evidence IDs |
| `reports` / `report_sections` | the emitted report, section by section |
| `runbook_chunks` | RAG corpus: content + FTS (`tsvector`, GIN) + embeddings (`vector(1024)`, HNSW) |
| `incidents_known` | labeled past incidents (signature → root cause) for future memory |

## 5. Local development stack

`make up` builds a Postgres 18 image with the pgvector extension compiled in (base image: `postgres:18-alpine`) and starts it with a healthcheck; `DATABASE_URL` and all other settings follow `config.py` with dev-safe defaults. Everything except the integration tests runs **without** the database and without any cloud credential or API key — cassettes in `fixtures/` are the default data path.

## 6. Planned evolution

1. **Live cloud probes** plug in behind the same `ProbeSource` interface (boto3 adapter behind the client seam), with per-environment read-only IAM credentials; cassettes remain the default for tests and demos.
2. **Real LLM node** for diagnose/narration behind the same node contracts, plus true live SSE streaming from `graph.stream()`.
3. **Offline eval harness** with golden scenarios, abstention scoring, and the poisoned-evidence prompt-injection case as a first-class test.
