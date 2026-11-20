# ATTRIBUTION.md

CloudInvestigator is an original implementation. This document records every
external contribution for academic integrity, in two kinds: **vendored code**
(file-level provenance, license obligations) and **borrowed patterns**
(design ideas learned from reading other projects — no code copied).

## 1. Vendored code: the retrieval pillar

The runbook RAG pillar (hybrid retrieval: pgvector + Postgres FTS, fused with
Reciprocal Rank Fusion) is vendored code from:

- **Upstream repository:** <https://github.com/lellisz/incident-copilot>
- **Commit:** `3b7e761b4feeec5b0451e4f1918b1493a44e00cb` (branch `main`)
- **License:** Apache-2.0. The full license text is shipped at
  `third_party/incident-copilot/LICENSE`, and the upstream `NOTICE` file at
  `third_party/incident-copilot/NOTICE`, as Apache-2.0 §4(d) requires.

Per Apache-2.0 §4, the redistributed files are unmodified except for
prominent provenance notices (§4(b): "You must cause any modified files to
carry prominent notices stating that You changed the files"), and the
LICENSE/NOTICE files accompany the distribution (§4(d)).

### Vendored verbatim

Upstream logic unchanged; a three-line provenance comment was added at the
top of each file:

| This repo | Upstream file |
|---|---|
| `src/cloud_investigator/retrieval/chunker.py` | `src/incident_copilot/rag/chunker.py` |
| `src/cloud_investigator/retrieval/fusion.py` | `src/incident_copilot/rag/fusion.py` |
| `src/cloud_investigator/retrieval/context.py` | `src/incident_copilot/rag/context.py` |
| `src/cloud_investigator/retrieval/__init__.py` | `src/incident_copilot/rag/__init__.py` |

`context.py` keeps upstream's logic unchanged; the only edits are the
provenance header every vendored file carries and importing `RetrievedChunk`
from the adapted `retriever.py` (the model kept its upstream shape, so no code
change was needed). "Verbatim" below therefore means *no behavioural edit*,
not byte equality.

### Reference copy (not executed)

`third_party/incident-copilot/rag.sql` — upstream's schema
(`src/incident_copilot/schema/rag.sql`), kept for reference only. The live
schema for this project's runbook store is our own DDL in
`migrations/002_runbooks.sql`, which defines different tables and columns.

### Modified / adapted

| This repo | Upstream file | Why it was modified |
|---|---|---|
| `src/cloud_investigator/retrieval/retriever.py` | `src/incident_copilot/rag/retriever.py` | Adapted to our `runbook_chunks` schema (`chunk_id TEXT`, `doc_path`/`section_path` columns, English FTS through the generated `tsv` column); configuration and the embedder are injected instead of module globals; no `structlog`/metrics, and no `pgvector` Python package (embeddings are sent as SQL text cast `::vector`). |
| `src/cloud_investigator/retrieval/ingest.py` | `src/incident_copilot/rag/ingest.py` | Adapted to our schema: deterministic chunk ids (`rb:{doc_slug}:{section_slug}:{sha256(content)[:8]}`) with idempotent `ON CONFLICT (chunk_id) DO UPDATE` upserts (upstream keyed on `content_hash` with `DO NOTHING`), plus `service_id`/`env` columns; no `structlog`, no `pgvector` package. |

### Rewritten (same role, new code)

| This repo | Upstream counterpart | Why it was rewritten |
|---|---|---|
| `src/cloud_investigator/retrieval/embeddings.py` | `src/incident_copilot/rag/embeddings.py` | Upstream coupled to Bedrock Titan, Ollama and Google embedding APIs. Rewritten as a small `Embedder` protocol with a deterministic offline default (`FakeEmbedder`, a blake2b bag-of-words hash — the idea of upstream's `_fake()` CI embedder) and the dimension injected by the caller. No network path, no AWS/LLM coupling. |
| `src/cloud_investigator/retrieval/rerank.py` | `src/incident_copilot/rag/rerank.py` | Upstream reranked via an LLM-judge through eight router files and LangChain. Rewritten as a pure function: `scorer=None` returns candidates in RRF order (the offline deterministic path), and an optional injected callable reranks in one batched call. No router, no LLM imports. |
| `src/cloud_investigator/retrieval/config.py` | upstream's `config.settings` singleton | Upstream read a module-level settings object at import time. Rewritten as a frozen parameter record mapped from this project's parsed settings — no module singleton, no import-time environment read. |

## 2. Patterns-only inspiration (no code borrowed)

The following projects informed CloudInvestigator's architecture through their
design patterns. No code was copied from any of them.

- **HolmesGPT** (Apache-2.0) — the structured tool-result envelope
  (NO_DATA ≠ ERROR), verbatim upstream errors in evidence, scope envelopes
  with default limits, and the SQL triple-guard plus
  `SET TRANSACTION READ ONLY`.
- **kagent** (Apache-2.0) — dual read-only control: an operation allowlist at
  registration time and least-privilege credentials at the provider layer.
- **OpenSRE** (Apache-2.0) — a typed source-resolution stage, noise
  short-circuiting, and validated claims as a first-class state element.
- **Flawless** (PolyForm license) — competing-hypothesis diagnosis (at least
  two ranked candidates, supporting and contradicting evidence, deterministic
  fallback). Patterns only; its license is not code-compatible with this
  project and no Flawless code was used.
- **mezmo/aura** — the recommendation shape: name the culprit version when the
  evidence identifies one, give an explicit rollback path, and state the steps
  that prove recovery. Reimplemented in `engine/recommend.py`.
- **SREGym** — the evaluation style: relay recorded incidents, score each
  report for correct component, correct fault class, share of claims backed by
  a real evidence id, and abstention where evidence is missing. Reimplemented
  in `eval/` and `scripts/run_evals.py`.

## 3. Provenance caveat

The upstream `incident-copilot` history is itself AI-assisted: its commits
carry `Co-Authored-By: Claude Sonnet 5` trailers (verified on commit
`3b7e761`). Provenance for the vendored pillar is therefore supported — by
the upstream repository, its license headers, and its commit trail — but not
independently provable beyond those artifacts. This project attributes the
pillar to that repository and commit, and claims as its own work only the
adaptations and rewrites listed above.
