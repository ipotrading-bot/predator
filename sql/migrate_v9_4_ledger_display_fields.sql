-- PREDATOR PAIM v9.4 — Migration (run once in Supabase SQL Editor)
--
-- Context: /performance only ever reads from ai_learning_ledger (by design —
-- `signals` is purged after 48h, ai_learning_ledger is the permanent record).
-- But the ledger never captured the odds, market label, or selection (pronostic)
-- that were actually on the signal at scan time — only `signals` had them, so
-- that information was lost forever the moment a terminal signal aged out of
-- the 48h window. This makes it impossible to ever answer "did the pick win?"
-- for anything older than 2 days.
--
-- This migration is idempotent: ADD COLUMN IF NOT EXISTS is a no-op if already applied.

ALTER TABLE public.ai_learning_ledger
  ADD COLUMN IF NOT EXISTS odds      numeric(6,2),
  ADD COLUMN IF NOT EXISTS selection text,
  ADD COLUMN IF NOT EXISTS market    text;

-- ============================================================
-- Post-migration checks (run manually, not part of the migration)
-- ============================================================
-- SELECT column_name FROM information_schema.columns
-- WHERE table_name = 'ai_learning_ledger' ORDER BY ordinal_position;
