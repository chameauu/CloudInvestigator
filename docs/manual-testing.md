# Manual testing guide

Everything below is executed by hand, in order, from a terminal. Each test
states what to run and what you should see. Total time: about 15 minutes for
the offline tests, plus 5–10 minutes if you also run the Docker stack (the
first image build compiles pgvector).

## 0. What you need

| Requirement | Needed for |
|---|---|
| `uv` (Astral) | every command — it installs Python 3.13 and the lockfile |
| A POSIX terminal | the make targets |
| Docker + Compose | only the "Full stack" section (optional) |
| A browser | only the "Browser demo" section (optional) |

No cloud account, no API key, no database is needed for the offline tests.
They replay recorded cassettes from `fixtures/`.

## 1. Install

```bash
cd CloudInvestigator
uv sync
```

**Expect:** packages resolve from `uv.lock` and install; exit code 0. A warm
cache takes about a second.

## 2. Test A — one typed report, offline

```bash
make run
```

**Expect:** a JSON `InvestigationReport` ending with:

```json
"abstained": false,
"abstain_reason": "",
"confidence": "high"
```

and, above it, `hypotheses`, a `findings` array with one high-severity
`AccessDenied` finding, four `evidence` items starting `ev:s3_access_denied:`,
and a `coverage` block listing `sources_planned` and `sources_queried`.

**Instructions for the mark (assignment demo):** `0` means "not tested",
`1` means "ran but wrong output", `2` means "ran as described".

## 3. Test B — terminal walkthrough, eight incidents

```bash
make demo                              # default: s3_access_denied
uv run python scripts/demo.py --list   # scenarios (`make demo --list` makes make eat the flag)
```

**Expect** for the default run, in order:

1. `[intake]` — investigation id, service `s3`, incident window.
2. `[plan]` — sources `cloudwatch_logs, sql` and a one-line rationale.
3. `[gate]` — catalog service `QA data-lake ingestion (S3)`, both sources
   marked `ok`, with log group, filter and database named.
4. `[probe_source]` lines — one per call with `ok`/`error`; the SQL query that
   references `s3_access_logs` shows a red verbatim error
   (`relation "s3_access_logs" does not exist`). This is intentional: a failed
   probe is recorded as evidence, not hidden.
5. `[hypothesize]` — three competing candidates, plus
   `stripped citation ev:s3_access_denied:sql:999 (no such evidence)`.
6. `[eliminate]` — `✗ rank 2: contradicted by ev:s3_access_denied:cloudwatch_logs:1`
   then `✓ rank 1: all supporting evidence present`.
7. `[verify]` — one finding, three hypotheses kept.
8. `REPORT` block — hypotheses (confirmed / refuted / unverified), the finding
   with its evidence ids and the retrieved runbook chunks that ground it, a
   recommendation containing `(culprit: v3)`, `Rollback:` and `Verify:`, and
   coverage (4 calls, 1 failure).

Now run each remaining scenario and compare the report block:

| Command | Expected outcome |
|---|---|
| `make demo SCENARIO=rds_connection_refused` | finding `AuthFailure`, resource `orders-db`, confidence high |
| `make demo SCENARIO=alb_unhealthy_targets` | finding `TargetHealthDegraded`, resource `public-api` |
| `make demo SCENARIO=ecs_task_oom` | finding `OOMKilled`, resource `batch-worker:42` |
| `make demo SCENARIO=poisoned_logs` | finding `TargetHealthDegraded`; **no** confirmed claim says `OOMKilled` |
| `make demo SCENARIO=rds_connection_exhaustion` | finding `ConnectionPoolExhausted`, resource `orders-db` (held-out) |
| `make demo SCENARIO=alb_target_port_mismatch` | finding `TargetPortMismatch`, resource `public-api` (held-out) |
| `make demo SCENARIO=insufficient_evidence` | `ABSTAINED — insufficient evidence — no confirmed finding survived verification`, confidence `low` |

**Negative check:**

```bash
make demo SCENARIO=nope
```

**Expect:** `unknown scenario 'nope'`, the list of eight available scenarios, and
a hint line — no Python traceback. Exit code 1 (`echo $?`).

## 4. Test C — browser demo

In one terminal:

```bash
make serve
```

**Expect:** uvicorn starts on `http://127.0.0.1:8000`. Open the page, submit the
pre-filled question (or type your own S3 access-denied question).

**Expect on the page:** the progress panel fills stage by stage
(`intake`, `plan`, `gate`, `probe_source`, …), then the green report panel shows
the JSON report and a confidence line.

API by hand, in a second terminal:

```bash
curl -s -X POST http://127.0.0.1:8000/api/investigate \
  -H 'content-type: application/json' \
  -d '{"question":"Why do uploads fail with AccessDenied?","scenario":"s3_access_denied"}' \
  | head -c 300
```

**Expect:** `{"investigation_id":"inv:s3_access_denied:<stamp>", …`. The stub
after the colon makes ids run-scoped: two runs of the same scenario are two
ledger rows, so the id changes on every POST. Capture it:

```bash
INV=$(curl -s -X POST http://127.0.0.1:8000/api/investigate \
  -H 'content-type: application/json' \
  -d '{"question":"Why do uploads fail with AccessDenied?","scenario":"s3_access_denied"}' \
  | python -c 'import json,sys; print(json.load(sys.stdin)["investigation_id"])')
curl -N "http://127.0.0.1:8000/api/investigate/$INV/events"
```

**Expect:** `data: {"node": "intake", …}`, then `plan`, `gate`, probe events,
…, finishing with `{"node": "done"}`. Stop with Ctrl-C.

**Negative checks:**

```bash
curl -s -o /dev/null -w '%{http_code}\n' \
  http://127.0.0.1:8000/api/investigate/does-not-exist/events      # expect 404

curl -s -o /dev/null -w '%{http_code}\n' -X POST \
  http://127.0.0.1:8000/api/investigate \
  -H 'content-type: application/json' -d '{"scenario":"nope"}'    # expect 404
```

Stop the server with Ctrl-C when done.

## 5. Test D — the eval harness

```bash
make eval
```

**Expect:** a six-row headline table, every row `ok` in `comp`, `fault`,
`abstain` and `replay`, `100.0%` validity; then a separate two-row held-out
table (also all `ok`), then:

```
component accuracy   : 100.0%
fault-class accuracy : 100.0%
evidence validity    : 100.0%
abstention rate      : 16.7%
replay determinism   : identical

EVALS OK — conformance replay: expected component/fault per scenario, deterministic replays, no forbidden claims; held-out: 2/2
```

The headline percentages cover the six scripted scenarios only; the held-out
rows are printed separately and can fail the run without inflating the
scoreboard.

Exit code 0.

## 6. Test E — determinism, by hand

Run the same scenario twice and compare the reports, ignoring only the two
run-local fields (creation timestamp and run-scoped investigation id):

```bash
uv run python - <<'EOF'
from cloud_investigator.engine.graph import run_investigation
from cloud_investigator.eval import same_report
a = run_investigation("", "s3_access_denied")
b = run_investigation("", "s3_access_denied")
print("identical:", same_report(a, b), "| findings:", len(a.findings))
EOF
```

**Expect:** `identical: True | findings: 1`.

Extra check, if you want it: run `make demo` twice and compare the `REPORT`
blocks visually — wording, ordering and evidence ids must match exactly. The
`investigation_id` differs by design (run-scoped `inv:<scenario>:<utc-stamp>`);
`make eval` ignores that field and `created_at` when it asserts determinism.

## 7. Test F — read-only guarantees, by hand

**F1. The SQL guard accepts a read and rejects a write:**

```bash
uv run python - <<'EOF'
from cloud_investigator.sources.sql_probe import assert_read_sql
print("guard:", assert_read_sql("SELECT 1"))
try:
    assert_read_sql("DELETE FROM evidence")
except Exception as exc:
    print("rejected:", type(exc).__name__, "|", str(exc)[:80])
EOF
```

**Expect:** `guard: SELECT 1 LIMIT 100` (a LIMIT is forced) and
`rejected: PermissionError | write verb 'DELETE' appears in statement (fail-closed)`.

**F2. Try a few statements yourself** — replace the string in the snippet above:

| Statement | Expected |
|---|---|
| `select * from evidence limit 5` | accepted, unchanged |
| `WITH x AS (SELECT 1) SELECT * FROM x` | accepted, `LIMIT 100` appended |
| `SELECT 1; DROP TABLE evidence` | rejected — not a single statement |
| `UPDATE evidence SET excerpt = 'x'` | rejected — write verb |
| `INSERT INTO evidence SELECT * FROM evidence` | rejected — write verb |
| `CREATE TEMP TABLE t AS SELECT 1` | rejected — write verb (fail-closed) |

**F3. The server-side backstop** (needs Docker): see the full-stack section;
`uv run pytest tests/integration -m integration -k read_only` proves that even
a writable connection refuses a `DELETE` (`sqlstate 25006`).

## 8. Test G — full stack (optional, needs Docker)

```bash
make up
```

**Expect:** compose builds `cloudinvestigator-db` from `postgres:18-alpine` with
pgvector compiled in (first build: several minutes, later builds: seconds), then
`db ready: postgresql://cloudinvestigator:***@localhost:5432/cloudinvestigator`.

```bash
make seed
```

**Expect:** migrations applied (or reported as already applied), then
`seed: runbooks files=4 chunks=9 upserts=9` and `seed: OK`. Running it again
prints the same chunk counts (idempotent upsert).

```bash
make test-integration
```

**Expect:** `20 passed` — schema round-trip, retrieval (including the offline /
pgvector parity check and a pipeline run through the pgvector index), and the probe
read-only guarantees, all green.

```bash
uv run python scripts/restart_drill.py
```

**Expect:** two runs with identical counts, then
`PASS — resumed report == fresh report; ledger rows match between both runs`
and exit code 0.

```bash
make down      # keeps the data volume
make reset     # DESTRUCTIVE: deletes the data volume too
```

**Expect:** `make down` stops the container; `make reset` removes container and
volume. After `make reset`, `make test-integration` must **fail loudly** with a
"run `make up` first" message — that is the designed behaviour (no silent
skips). Re-run `make up && make seed` to restore.

## 9. Test H — quality gates

```bash
make test                # offline unit + contract tests
make verify              # lint + tests + smoke + eval
uv run pre-commit run --all-files
```

**Expect:** all tests pass (340 offline tests at the time of writing); `make
verify` ends with `EVALS OK`, exit code 0; every pre-commit hook passes.

## 10. Test I — adversarial cases

0. **Unresolvable question abstains** — submit a question with no known
   service vocabulary, e.g. `{"question":"why is checkout slow?"}` with no
   scenario. Expect an abstaining report whose `abstain_reason` says the question
   has no known service, resource or fault vocabulary, zero findings, and zero
   probes: there is no silent default scenario.

| Case | How | Expected |
|---|---|---|
| Poisoned evidence | `make demo SCENARIO=poisoned_logs` | the injected sentence appears only in an *unverified* hypothesis; the finding is the real ALB fault; no claim cites `ev:poisoned_logs:sql:999` |
| Insufficient evidence | `make demo SCENARIO=insufficient_evidence` | abstains with a reason, no invented finding |
| Failed probe is visible | default demo | the `s3_access_logs` error is printed and listed in coverage `failures`, never hidden |
| Unknown scenario | `make demo SCENARIO=nope` / API | clean message + 404, no traceback / 500 |
| Unknown investigation id | `GET /api/investigate/xxx/events` | 404 |

## 11. Checklist

- [ ] `uv sync` succeeds
- [ ] `make run` → typed report, confidence high
- [ ] `make demo` → stages, eliminations, cited report
- [ ] all eight `SCENARIO=` runs match the expected outcome table
- [ ] unknown scenario → clean error, exit 1
- [ ] `make serve` → page loads, stream fills, report panel shows JSON
- [ ] `POST /api/investigate` → 200 with report; SSE stream reaches `done`
- [ ] `GET …/events` for an unknown id → 404
- [ ] `make eval` → 100% / 100% / 100%, replays identical, `EVALS OK`
- [ ] determinism snippet → `identical: True`
- [ ] guard accepts SELECT, rejects DELETE (and the other statements)
- [ ] `make up` + `make seed` + `make test-integration` + `restart_drill.py` → PASS
- [ ] `make reset` then integration tests fail loudly (by design)
- [ ] `make test`, `make verify`, `pre-commit run --all-files` → green
- [ ] poisoned and abstention scenarios behave as described

## 12. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `make test-integration` errors with "run `make up` first" | stack not running | `make up` |
| `make up` fails at `db NOT reachable` | first build still compiling, or port 5432 busy | check `docker compose logs db`; free port 5432 or change the published port in `docker-compose.yml` |
| `make serve` fails: address already in use | port 8000 busy | `uv run uvicorn cloud_investigator.api.main:app --port 8001` |
| `FileNotFoundError: cassette_…json` on an older build | pre-robustness version | run `git pull`; current code prints available scenarios |
| `make seed` says 0 chunks | `runbooks/` missing or empty | run from the repository root |
| `pre-commit` not found | hooks not installed | `uv run pre-commit install` (or run `uv run pre-commit run --all-files` directly) |

## 13. Where to look when something fails

- The failing command's own output — every script prints a verbatim error and
  exits non-zero.
- `fixtures/cassette_<scenario>.json` — what the scenario scripts; compare the
  report against it.
- `fixtures/probes/*.json` — what the probes replay.
- `evals/golden.yaml` — the scored expectation for each scenario.
- `catalog.yaml` — which resources and probe bindings a service has.
