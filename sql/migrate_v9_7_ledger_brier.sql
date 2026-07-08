-- PREDATOR PAIM v9.5 — Migration (run once in Supabase SQL Editor)
--
-- Context (Task 4): a Brier score needs (predicted_probability, outcome)
-- pairs, but ai_learning_ledger never captured the model's own predicted
-- probability (sharp_prob, already present on `signals` since
-- migrate_v9_2) — only initial_edge (a price ratio, not a probability).
-- Without this column the dashboard's calibration-bucket reliability
-- diagram (core/stats_utils.bucket_predictions) has nothing to bucket by.
--
-- This migration is idempotent: ADD COLUMN IF NOT EXISTS is a no-op if
-- already applied. It does nothing until run manually in the Supabase
-- SQL Editor — committing it to git does NOT apply it to the DB.

ALTER TABLE public.ai_learning_ledger
  ADD COLUMN IF NOT EXISTS sharp_prob numeric(6,4);

-- No backfill: sharp_prob was never captured on ai_learning_ledger rows
-- before this migration, and there's no reliable join back to `signals`
-- for rows old enough to have already been purged from there (48h window)
-- — left NULL rather than fabricated. core/stats_utils.bucket_predictions
-- callers already skip rows with no sharp_prob.

-- ============================================================
-- Post-migration check (run manually, not part of the migration)
-- ============================================================
-- SELECT column_name FROM information_schema.columns
-- WHERE table_name = 'ai_learning_ledger' AND column_name = 'sharp_prob';
