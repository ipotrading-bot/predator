-- PREDATOR PAIM v9.2 — Migration (run once in Supabase SQL Editor)
-- Formalizes columns that `run_engine.py`, `core/audit_engine.py`,
-- `core/settlement.py` and `backfill_ledger.py` already read/write on
-- `signals` and `ai_learning_ledger` but that were never captured in a
-- committed sql/migrate_*.sql (added directly in the Supabase dashboard
-- at some point). This migration is idempotent: if a column already
-- exists, `ADD COLUMN IF NOT EXISTS` is a no-op.
--
-- IMPORTANT — this file does nothing until you run it manually in the
-- Supabase SQL Editor. Committing it to git does NOT apply it to the DB.

-- ============================================================
-- 1. signals — missing columns
-- ============================================================
ALTER TABLE public.signals
  ADD COLUMN IF NOT EXISTS market_key       text,
  ADD COLUMN IF NOT EXISTS sharp_prob       numeric(6,4),
  ADD COLUMN IF NOT EXISTS match_time       timestamptz,
  ADD COLUMN IF NOT EXISTS match_id         text,
  ADD COLUMN IF NOT EXISTS selection_name   text,
  ADD COLUMN IF NOT EXISTS kelly_pct        numeric(6,2),
  ADD COLUMN IF NOT EXISTS advice           text,
  ADD COLUMN IF NOT EXISTS sharp_sources    text,
  ADD COLUMN IF NOT EXISTS consensus_score  integer,
  ADD COLUMN IF NOT EXISTS clv_pct          numeric(6,2),
  ADD COLUMN IF NOT EXISTS closing_line     numeric(6,2),
  ADD COLUMN IF NOT EXISTS closed_at        timestamptz,
  ADD COLUMN IF NOT EXISTS outcome          text;

-- ── Backfill (deterministic, no bare DEFAULT) ──────────────────────
-- sharp_prob is CRITICAL: `_purge_old_signals()` in run_engine.py runs
-- `.is_("sharp_prob", "null")` on every scan cycle with NO status filter.
-- If this column is added and left NULL on existing rows, the very next
-- purge cycle deletes every pre-existing signal (active AND terminal).
-- Backfilling to 1.0 ("legacy row, confidence unknown") keeps them out
-- of that purge rule without fabricating a false-negative confidence.
UPDATE public.signals
SET sharp_prob = 1.0
WHERE sharp_prob IS NULL;

-- market_key: recoverable from the existing `market` label format
-- (market_label() in core/paim_engine.py: totals → "Over"/"Under" in
-- the label, spreads → " PS " token, h2h → everything else).
UPDATE public.signals
SET market_key = CASE
  WHEN market ILIKE '%Over%' OR market ILIKE '%Under%' THEN 'totals'
  WHEN market ILIKE '% PS %' OR market ILIKE '% PS+%' OR market ILIKE '% PS-%' THEN 'spreads'
  ELSE 'h2h'
END
WHERE market_key IS NULL;

-- selection_name / match_id: mirror the same fallback the app code
-- already applies at write time (`selection_name or name`, `match_id
-- or ""`), so legacy rows read identically to how the app already
-- treats a missing value.
UPDATE public.signals
SET selection_name = match
WHERE selection_name IS NULL;

UPDATE public.signals
SET match_id = ''
WHERE match_id IS NULL;

-- kelly_pct, advice, sharp_sources, consensus_score, clv_pct,
-- closing_line, closed_at, outcome, match_time: deliberately left
-- NULL for legacy rows. None of them are read by any purge/discard
-- filter (verified against run_engine.py's purge_rules and _emit()),
-- and none can be derived from other columns without fabricating a
-- number that would corrupt CLV/outcome-based analytics on the
-- dashboard and the learning layer. NULL here means "not computed
-- before this migration", which is the true state.

-- ============================================================
-- 2. ai_learning_ledger — missing columns
-- ============================================================
ALTER TABLE public.ai_learning_ledger
  ADD COLUMN IF NOT EXISTS market_type            text,
  ADD COLUMN IF NOT EXISTS time_to_match_minutes   integer,
  ADD COLUMN IF NOT EXISTS initial_edge            numeric(6,2),
  ADD COLUMN IF NOT EXISTS sharp_divergence_std     numeric(8,4),
  ADD COLUMN IF NOT EXISTS clv_final               numeric(6,2),
  ADD COLUMN IF NOT EXISTS was_clv_positive        boolean;

-- ── Backfill ────────────────────────────────────────────────────────
-- was_clv_positive is safely derivable wherever clv_final is already
-- populated — it's a pure function of that column, not a guess.
UPDATE public.ai_learning_ledger
SET was_clv_positive = (clv_final > 0)
WHERE was_clv_positive IS NULL
  AND clv_final IS NOT NULL;

-- market_type, time_to_match_minutes, initial_edge,
-- sharp_divergence_std, clv_final: no committed migration ever created
-- this table (see note below), so there is no other column in
-- ai_learning_ledger to derive these from, and no `signals.signal_id`
-- join is guaranteed for the oldest rows (signal_id itself was added
-- in migrate_v8_9.sql). Left NULL — this table is append-only
-- (grepped: no .delete()/purge call targets ai_learning_ledger
-- anywhere in the codebase), so NULL here carries no deletion risk,
-- unlike signals.sharp_prob above.

-- NOTE: sql/ has no CREATE TABLE for ai_learning_ledger at all — only
-- migrate_v8_9.sql ALTERs it assuming it pre-exists. If this table was
-- created ad hoc in the Supabase dashboard, consider writing a
-- companion `sql/setup_ai_learning_ledger.sql` (out of scope here).

-- ============================================================
-- 3. Post-migration checks (run manually, not part of the migration)
-- ============================================================
-- Confirm no signal is now exposed to the sharp_prob purge rule:
--   SELECT count(*) FROM public.signals WHERE sharp_prob IS NULL;        -- expect 0

-- Sanity-check the market_key backfill distribution:
--   SELECT market_key, count(*) FROM public.signals GROUP BY market_key; -- expect h2h/totals/spreads only, no NULL

-- Confirm both tables now expose every column the app code references:
--   SELECT table_name, column_name, data_type
--   FROM information_schema.columns
--   WHERE table_name IN ('signals', 'ai_learning_ledger')
--   ORDER BY table_name, ordinal_position;
