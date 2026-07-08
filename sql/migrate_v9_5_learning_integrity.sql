-- PREDATOR PAIM v9.5 — Migration (run once in Supabase SQL Editor)
--
-- Context: core/learning_layer.py used to derive hit_rate/threshold
-- adjustments from ai_learning_ledger.clv_final, which for
-- core/settlement.py's settle_signal() rows is a re-derivation of the
-- entry edge (xbet_odd/pinnacle_price — the exact same scan-time prices
-- already stored as `initial_edge`), not a real closing-line comparison.
-- Since MIN_EDGE only ever lets positive-edge signals through, clv_final
-- was ~always >= 0 regardless of the real match result — the learning
-- loop never actually learned from win/loss. Fixed in code to key off
-- `outcome` instead; this migration adds the one column needed for the
-- accompanying real, stake-weighted ROI calculation.
--
-- This migration is idempotent: ADD COLUMN IF NOT EXISTS is a no-op if
-- already applied. It does nothing until run manually in the Supabase
-- SQL Editor — committing it to git does NOT apply it to the DB.

ALTER TABLE public.ai_learning_ledger
  ADD COLUMN IF NOT EXISTS kelly_pct numeric(6,2);

-- No backfill: kelly_pct wasn't captured on `signals` rows that already
-- aged out of the 48h window before this migration, so historical ledger
-- rows correctly stay NULL ("stake unknown for this bet") rather than
-- fabricating a value. core/learning_layer.py's ROI calc already skips
-- rows with a NULL kelly_pct.

-- ============================================================
-- Post-migration check (run manually, not part of the migration)
-- ============================================================
-- SELECT column_name FROM information_schema.columns
-- WHERE table_name = 'ai_learning_ledger' ORDER BY ordinal_position;
