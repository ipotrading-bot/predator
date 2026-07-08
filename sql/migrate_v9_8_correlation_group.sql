-- PREDATOR PAIM v9.5 — Migration (run once in Supabase SQL Editor)
--
-- Context (Task 5): core/paim_engine.correlation_group() tags every signal
-- with "{sport}:{date}:{league}" so core/tax_engine.suggest_system() can
-- refuse (default) or discount (Gaussian-copula mode) combining two legs
-- that aren't safely independent — e.g. two markets on the same match.
--
-- This migration is idempotent: ADD COLUMN IF NOT EXISTS is a no-op if
-- already applied. It does nothing until run manually in the Supabase
-- SQL Editor — committing it to git does NOT apply it to the DB. Until
-- applied, run_engine.py's insert retries once with this column stripped
-- (see _OPTIONAL_COLS in run_engine.py) — signals still save, just without
-- the correlation tag, meaning suggest_system() cannot detect correlation
-- for those rows.

ALTER TABLE public.signals
  ADD COLUMN IF NOT EXISTS correlation_group text;

-- No backfill: recoverable from sport/league/match_time for any row where
-- those are already populated, but not worth a computed backfill here —
-- purged signals age out within 48h, and settled/closed/expired history
-- never re-enters suggest_system()'s window-grouping anyway.

-- ============================================================
-- Post-migration check (run manually, not part of the migration)
-- ============================================================
-- SELECT column_name FROM information_schema.columns
-- WHERE table_name = 'signals' AND column_name = 'correlation_group';
