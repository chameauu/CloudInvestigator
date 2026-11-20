-- 003_evidence_scope.sql — scope evidence rows per investigation.
--
-- Evidence ids are scenario-stable by design (`ev:{scenario}:{source}:{seq}`),
-- so a replay cites the same ids. Investigation ids are run-scoped, which means
-- the same evidence id is legitimately written once per execution: the primary
-- key must therefore be `(investigation_id, id)`, not `id` alone.
--
-- 001_core.sql already creates the composite key; this migration exists for
-- databases created before that change (ALTER is a no-op on new ones).

DO $$
DECLARE
  current_def text;
BEGIN
  SELECT pg_get_constraintdef(oid) INTO current_def
  FROM pg_constraint
  WHERE conrelid = 'evidence'::regclass AND contype = 'p';

  IF current_def IS DISTINCT FROM 'PRIMARY KEY (investigation_id, id)' THEN
    ALTER TABLE evidence DROP CONSTRAINT IF EXISTS evidence_pkey;
    ALTER TABLE evidence ADD CONSTRAINT evidence_pkey PRIMARY KEY (investigation_id, id);
  END IF;
END $$;
