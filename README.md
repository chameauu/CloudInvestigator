# CloudInvestigator

CloudInvestigator is a read-only investigation engine for recorded cloud
incidents, built around a trust architecture rather than around a model. A
LangGraph pipeline resolves a problem described in natural language to a
recorded scenario, decides from a service catalog which resources are in scope,
probes the registered sources through strictly read-only adapters, ranks
competing explanations, eliminates them as evidence arrives, and emits an
evidence-cited report — with abstention when the question or the evidence does
not support a conclusion.

**Scope, stated plainly:** the probes replay committed cassettes, and
question-to-scenario selection is deterministic vocabulary routing, not model
inference. Live cloud access and LLM reasoning are not implemented — see
[Scope and limitations](#scope-and-limitations).

## Quickstart (fully offline demo)

```bash
uv sync
make run          # one typed InvestigationReport — no credentials, no API keys
make demo         # terminal click-through: stages, eliminations, cited report
make serve        # browser demo: POST /api/investigate + SSE progress
```

Then open <http://127.0.0.1:8000/>: submit the natural-language problem, watch
the investigation stream node-by-node, and read the final report. The demo
replays deterministic cassettes in `fixtures/` — eight recorded incidents: six
scripted ones (S3 access denied, refusing database credentials, unhealthy
load-balancer targets, an OOM-killed task, a log line carrying an injected
instruction, and a run with no usable data) plus two held-out ones used by the
eval split (RDS connection-pool exhaustion, ALB target-port mismatch). Pick one
with `make demo SCENARIO=ecs_task_oom`.

Each run resolves the service against `catalog.yaml` to decide which resources
are in scope and where to look, probes the sources that are actually
registered, then ranks at least two competing explanations — showing each one
confirmed or eliminated as the evidence arrives. Recommendations carry the
culprit version when the evidence names one, an explicit rollback path, and
the steps that prove recovery. A presenter-ready walkthrough is in
[`docs/demo.md`](docs/demo.md), and a hands-on test script (every command, its
expected output, and a checklist) is in
[`docs/manual-testing.md`](docs/manual-testing.md).

## Scope and limitations

This is an offline replay harness with a production-shaped trust layer. It is
honest about the difference:

- **Question interpretation is routing, not reasoning.** The question is scored
  against the vocabulary of the recorded scenarios (services, resources, fault
  classes). A unique, specific match selects the scenario; anything else
  abstains with the reason. There is deliberately no silent default scenario.
  A model-driven planner is the intended replacement behind the same seam.
- **Probes replay hand-authored cassettes.** No live cloud API is called. The
  client protocols (`LogsClient`, `SqlClient`) are the seam a live adapter
  plugs into; none is wired.
- **One environment.** `catalog.yaml` describes a single development
  environment; Dev/QA separation is not represented.
- **Runbook retrieval is a tested library, not a pipeline stage.** Hybrid
  retrieval (pgvector + full-text + RRF) and ingestion work and are covered by
  tests, but no graph node invokes retrieval; report citations are authored
  with the scenario.
- **Read-only is enforced in the application, not by IAM.** The prefix
  allowlist, the SQL triple guard, `SET TRANSACTION READ ONLY`, and statement
  timeouts are real and tested. A dedicated read-only cloud credential policy
  is required in any live deployment and is not implemented or proven here.
- **The eval is a conformance test, plus a small held-out check.** Expectations,
  cassettes, and scripts for the six headline rows were authored together, so
  their 6/6 score proves the harness faithfully replays what it was given. Two
  further incidents were authored from their stories after the scoring rules
  were frozen; they are replayed and scored but reported separately, excluded
  from the headline metrics, and a held-out miss still fails the run. Two
  headline rows carry signal independent of the script: the poisoned-evidence
  row (the citation guard demotes regardless) and the abstention row (a
  genuine code path).
- **No model in the loop.** Diagnosis runs through a cassette diagnoser and a
  deterministic fallback; the `Diagnoser` protocol is where a real model
  attaches, and the fallback is its measurable baseline.
- **Investigation identity is run-scoped** (`inv:<scenario>:<utc-stamp>`), so
  re-running a scenario records a new run with its own ledger rows instead of
  overwriting the previous audit trail; the checkpoint thread stays
  scenario-level so a resume continues the interrupted run.

## Local stack

```bash
make up               # Postgres 18 + pgvector (Docker); waits until ready
make seed             # apply migrations + ingest the runbook corpus
make test             # offline unit + contract tests
make test-integration # integration tests against the running stack
make verify           # lint + offline tests + end-to-end smoke + conformance eval
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

## Evaluated behaviour

`make eval` replays eight recorded scenarios and scores each report: correct
component, correct fault class, share of claims whose evidence ids actually
resolve, abstention where evidence is missing, and identical output across two
replays. Injected claims are checked too: a report fails if a term planted in
the logs appears in a finding or a confirmed hypothesis.

Six are **headline** rows — S3 access denied, refusing database credentials,
unhealthy load-balancer targets, an OOM-killed task, a log line carrying an
injected instruction, and a run with no usable data — and their metrics form
the scoreboard. Two are **held-out** rows — RDS connection-pool exhaustion and
an ALB target-port mismatch — authored from their incident stories after the
scoring rules were frozen, never tuned against. They are printed in their own
table, excluded from the headline percentages, and a held-out failure still
exits non-zero.
