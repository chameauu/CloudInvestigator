-- requires-extension: vector
-- 002_runbooks.sql — RAG runbook store + known-incident memory.
--
-- REQUIRES pgvector: `scripts/migrate.py` applies this file only when
--   SELECT 1 FROM pg_available_extensions WHERE name = 'vector'
-- says the extension exists in this Postgres installation (001_core.sql runs
-- on plain Postgres regardless). Idempotent by construction.

CREATE EXTENSION IF NOT EXISTS vector;

-- Runbook markdown chunks (hybrid retrieval: pgvector + Postgres FTS).
CREATE TABLE IF NOT EXISTS runbook_chunks (
  id            bigserial PRIMARY KEY,
  chunk_id      text UNIQUE,
  doc_path      text,
  section_path  text,
  service_id    text,
  env           text,
  updated_at    timestamptz,
  content       text NOT NULL,
  embedding     vector(1024),
  tsv           tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED
);
CREATE INDEX IF NOT EXISTS runbook_chunks_embedding_hnsw_idx
  ON runbook_chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS runbook_chunks_tsv_gin_idx
  ON runbook_chunks USING gin (tsv);

-- Memory of past labeled incidents. `report_id` deliberately has NO FK:
-- known-incident memory must outlive the deletion of a report row.
CREATE TABLE IF NOT EXISTS incidents_known (
  id            uuid PRIMARY KEY,
  signature     text,
  service_id    text,
  root_cause    text,
  remediation   text,
  occurred_at   timestamptz,
  report_id     bigint
);
