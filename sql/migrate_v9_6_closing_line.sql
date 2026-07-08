-- PREDATOR PAIM v9.5 — Migration (run once in Supabase SQL Editor)
--
-- Context (Task 3): the only "CLV" the system had before this was either
-- a re-derivation of the entry edge from the exact same scan-time price
-- (core/settlement.py — see sql/migrate_v9_5_learning_integrity.sql), or
-- a price fetched hours-to-days after kickoff during the 6h audit pass
-- (core/audit_engine.py's Pass 2) — neither is a genuine closing line.
-- This migration adds the columns for core/audit_engine.py's new
-- capture_closing_lines(), which re-fetches the real Pinnacle consensus
-- price ~5 minutes before kickoff — the actual closing line — on its own
-- hourly job (run_closing_line.py / .github/workflows/closing_line.yml),
-- independent of and available well before the match's real WIN/LOSS
-- settlement.
--
-- This migration is idempotent: ADD COLUMN IF NOT EXISTS is a no-op if
-- already applied. It does nothing until run manually in the Supabase
-- SQL Editor — committing it to git does NOT apply it to the DB.

ALTER TABLE public.signals
  ADD COLUMN IF NOT EXISTS closing_pinnacle_price numeric(8,4),
  ADD COLUMN IF NOT EXISTS clv_pct_real           numeric(6,2);

ALTER TABLE public.ai_learning_ledger
  ADD COLUMN IF NOT EXISTS closing_pinnacle_price numeric(8,4),
  ADD COLUMN IF NOT EXISTS clv_pct_real           numeric(6,2);

-- No backfill: closing_pinnacle_price/clv_pct_real can only ever be
-- captured in the ~5-minute pre-kickoff window by design — there is no
-- way to retroactively compute a real closing line for a match that has
-- already kicked off, so historical rows correctly stay NULL rather than
-- fabricating a value from the audit-time proxy price.

-- ============================================================
-- Post-migration check (run manually, not part of the migration)
-- ============================================================
-- SELECT column_name FROM information_schema.columns
-- WHERE table_name IN ('signals', 'ai_learning_ledger')
--   AND column_name IN ('closing_pinnacle_price', 'clv_pct_real');
