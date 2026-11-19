# Demo

A ten-minute walkthrough of CloudInvestigator. Everything runs from a clean
checkout, offline: recorded cassettes, no cloud credentials, no API keys, no
database.

## Setup

```bash
uv sync           # ~1 s warm, resolves the locked environment
make demo         # terminal walkthrough, default scenario
make serve        # optional: browser view on http://127.0.0.1:8000/
```

Six recorded incidents are available:

| Scenario | What it is |
|---|---|
| `s3_access_denied` | A deliberate Deny in a bucket policy blocks the ingest uploader |
| `rds_connection_refused` | The app is still using the previous database secret |
| `alb_unhealthy_targets` | A deploy left one of three targets failing its health check |
| `ecs_task_oom` | A batch task is killed at its memory ceiling |
| `poisoned_logs` | A real fault, with an instruction planted in a log line |
| `insufficient_evidence` | No usable data — the correct answer is abstention |

Pick one with `make demo SCENARIO=ecs_task_oom`.

## Presenter script

1. **Framing (30 s).** "Given a problem in natural language, the system decides
   which sources to read, identifies the resources involved, probes them through
   strictly read-only adapters, and produces a report where every claim cites
   evidence a tool captured."

2. **The problem.** Show the question, e.g. *"Since this morning the QA
   data-lake ingestion job fails: uploads to the qa-data-lake S3 bucket are
   rejected with AccessDenied. Why?"*

3. **Source selection.** `[gate]` resolves the service against `catalog.yaml`:
   which log group, which filter, which authorized database, which resources are
   in scope. Sources that are not connected are reported with a reason — never
   silently dropped.

4. **Probing.** Each `[probe_source]` line is a tool call with its status and
   duration. Point at the failing SQL query: the verbatim upstream error is
   recorded as evidence, not hidden, and NO_DATA is distinct from an error.

5. **Competing explanations.** `[hypothesize]` lists at least two ranked
   candidates. One cites a fabricated evidence id; the deterministic check
   strips it on the spot.

6. **Elimination (the money shot).** `[eliminate]` replays evidence arrival:
   `✗ rank 2: contradicted by ev:…:cloudwatch_logs:1` — the network-path
   hypothesis dies when the DENY audit row arrives. `✓ rank 1` confirms when its
   last supporting item lands. Statuses are decided by code, not by the model.

7. **The report.** A typed `InvestigationReport`: hypotheses with their final
   statuses, findings with evidence ids and a runbook citation, recommendations
   with the culprit version, a rollback path and the steps that prove recovery,
   and a coverage block listing what was queried and what failed.

8. **Abstention.** `make demo SCENARIO=insufficient_evidence`: no probes return
   data, so the system abstains with a reason instead of inventing a cause.

9. **Browser view (optional).** `make serve`, open `http://127.0.0.1:8000/`,
   submit the question: the same stages stream over SSE, including the
   elimination events as they happen.

10. **Close.** "Same input twice gives a byte-identical report; the harness
    scores six scenarios offline in about a second."

## The scoreboard (a conformance check, not an accuracy benchmark)

```
scenario                   component  fault class           claims cited
s3_access_denied           s3         AccessDenied          2/2
rds_connection_refused     rds        AuthFailure           2/2
alb_unhealthy_targets      alb        TargetHealthDegraded  2/2
ecs_task_oom               ecs        OOMKilled             2/2
poisoned_logs              alb        TargetHealthDegraded  2/2
insufficient_evidence      —          —                     abstained, correctly
```

- **6/6** scenarios replay with the expected component and fault class. The
  expectations were authored alongside the cassettes, so this measures that the
  harness reproduces the script — not diagnostic accuracy.
- **100%** of actionable claims cite evidence ids that resolve to items a probe
  produced; the poisoned scenario's injected claim never reaches a finding.
- **Replay determinism**: every scenario replayed twice produces an identical
  report (compared field by field, ignoring only the creation timestamp).
- **Cost per investigation, offline: $0.00** — cassette replay, no model calls,
  no network. A hosted model, when wired into the diagnose stage, would add one
  bounded call per investigation (≤ 40 evidence items × ≤ 2000 characters ≈
  6–10k input tokens plus ~1k output). At typical hosted prices that is on the
  order of **$0.02–0.05 per investigation** — an estimate, not a measured
  figure; the offline path remains free and is what the demo uses.

Reproduce with `make eval`.
