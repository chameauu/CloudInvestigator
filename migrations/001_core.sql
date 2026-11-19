-- 001_core.sql — CloudInvestigator core ledger (investigations/evidence/tool_calls/hypotheses/reports).
--
-- Runs on PLAIN Postgres (no extensions) so `scripts/migrate.py` works against
-- postgres:18-alpine too. pgvector-dependent objects live in 002_runbooks.sql.
-- Idempotent by construction: every statement uses IF NOT EXISTS — re-apply is safe.
--
-- ID convention (differs from the uuid blueprint on purpose): investigations and
-- evidence ids are the TEXT ids minted by the application (`inv:{scenario}`,
-- `ev:{scenario}:{source}:{seq}`), so a replayed cassette addresses the same rows.

CREATE TABLE IF NOT EXISTS services (
  id            text PRIMARY KEY,
  env           text NOT NULL,
  description   text,
  architecture  text,
  owner         text,
  spec          jsonb NOT NULL
);

CREATE TABLE IF NOT EXISTS investigations (
  id          text PRIMARY KEY,
  question    text NOT NULL,
  service_id  text REFERENCES services(id),
  env         text NOT NULL DEFAULT 'dev',
  status      text NOT NULL DEFAULT 'running'
              CHECK (status IN ('running', 'done', 'abstained', 'failed')),
  plan        jsonb,
  budget      jsonb,
  summary     text,
  confidence  numeric(3,2),
  started_at  timestamptz DEFAULT now(),
  finished_at timestamptz
);
CREATE INDEX IF NOT EXISTS investigations_status_idx ON investigations (status);

-- One row = one verifiable fact. Excerpts are capped at 2000 chars by the
-- probe layer (`cap_excerpt`) and enforced again in the ledger code; the
-- column is deliberately plain text (no DB CHECK) so raw payloads are never
-- silently rejected by storage — an over-long excerpt is a code bug to fix.
CREATE TABLE IF NOT EXISTS evidence (
  id                text PRIMARY KEY,
  investigation_id  text NOT NULL REFERENCES investigations(id) ON DELETE CASCADE,
  source_type       text NOT NULL,
  resource_ref      text,
  ts_start          timestamptz,
  ts_end            timestamptz,
  excerpt           text NOT NULL,
  detail            jsonb,
  query_executed    text,
  query_hash        text,
  tool_call_seq     int
);
CREATE INDEX IF NOT EXISTS evidence_investigation_idx ON evidence (investigation_id);

-- Audit + budget + replay trail. `call_key` is the natural idempotency key
-- (md5 over source/operation/params/status/error) so re-persisting the same
-- run cannot duplicate audit rows, while two genuinely different executions
-- still get two rows. One ToolCallRecord = one row, always.
CREATE TABLE IF NOT EXISTS tool_calls (
  id                bigserial PRIMARY KEY,
  investigation_id  text NOT NULL REFERENCES investigations(id) ON DELETE CASCADE,
  source            text,
  operation         text,
  params            jsonb,
  scope             jsonb,
  status            text NOT NULL DEFAULT 'ok'
                    CHECK (status IN ('ok', 'error', 'skipped', 'denied', 'skipped_budget')),
  duration_ms       int,
  rows_returned     int,
  truncated         bool,
  error             text,
  created_at        timestamptz DEFAULT now(),
  call_key          text NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS tool_calls_call_key_idx
  ON tool_calls (investigation_id, call_key);
CREATE INDEX IF NOT EXISTS tool_calls_investigation_idx ON tool_calls (investigation_id);

-- Ranked probable causes. id is derived (`{investigation_id}:h{rank:02d}`) and
-- is the idempotency key across re-persists (verify may demote/re-rank).
-- evidence_ids are TEXT evidence ids (not FKs) so a hypothesis can cite
-- an id that verify later rejects — demotion must stay auditable.
CREATE TABLE IF NOT EXISTS hypotheses (
  id                text PRIMARY KEY,
  investigation_id  text NOT NULL REFERENCES investigations(id) ON DELETE CASCADE,
  rank              int NOT NULL,
  title             text,
  statement         text NOT NULL,
  rationale         text,
  confidence        numeric(3,2),
  status            text NOT NULL DEFAULT 'unverified'
                    CHECK (status IN ('confirmed', 'refuted', 'unverified')),
  arguments_for     jsonb NOT NULL DEFAULT '[]',
  arguments_against jsonb NOT NULL DEFAULT '[]',
  evidence_ids      text[] NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS hypotheses_investigation_idx ON hypotheses (investigation_id);

CREATE TABLE IF NOT EXISTS reports (
  id                bigserial PRIMARY KEY,
  investigation_id  text NOT NULL REFERENCES investigations(id) ON DELETE CASCADE,
  created_at        timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS reports_investigation_idx ON reports (investigation_id);

-- The typed report, stored as addressable sections (content = jsonb section
-- payload) so `load_report()` can round-trip the full InvestigationReport.
CREATE TABLE IF NOT EXISTS report_sections (
  report_id   bigint REFERENCES reports(id) ON DELETE CASCADE,
  section_id  text NOT NULL,
  title       text,
  content     jsonb,
  PRIMARY KEY (report_id, section_id)
);
