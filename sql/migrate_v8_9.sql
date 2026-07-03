-- PREDATOR PAIM v8.9 — Migration (run once in Supabase SQL Editor)
-- Fixes: ai_learning_ledger missing columns + win_rate view for performance analysis

-- 1. Add missing columns to ai_learning_ledger
ALTER TABLE public.ai_learning_ledger
  ADD COLUMN IF NOT EXISTS match       text,
  ADD COLUMN IF NOT EXISTS league      text,
  ADD COLUMN IF NOT EXISTS outcome     text,
  ADD COLUMN IF NOT EXISTS signal_id   bigint REFERENCES public.signals(id) ON DELETE SET NULL;

-- 2. RLS on ai_learning_ledger (if not already set)
ALTER TABLE public.ai_learning_ledger ENABLE ROW LEVEL SECURITY;
CREATE POLICY IF NOT EXISTS "ledger_read"   ON public.ai_learning_ledger FOR SELECT USING (true);
CREATE POLICY IF NOT EXISTS "ledger_insert" ON public.ai_learning_ledger FOR INSERT WITH CHECK (true);

-- 3. Win rate view — monthly breakdown from ledger
CREATE OR REPLACE VIEW public.win_rate_monthly AS
SELECT
  date_trunc('month', created_at)::date                                    AS month,
  sport,
  count(*)                                                                  AS total,
  count(*) FILTER (WHERE outcome = 'WIN')                                   AS wins,
  count(*) FILTER (WHERE outcome = 'LOSS')                                  AS losses,
  count(*) FILTER (WHERE outcome = 'PUSH')                                  AS pushes,
  count(*) FILTER (WHERE outcome = 'expired')                               AS expired,
  round(
    100.0 * count(*) FILTER (WHERE outcome = 'WIN')
    / NULLIF(count(*) FILTER (WHERE outcome IN ('WIN','LOSS')), 0), 1
  )                                                                          AS win_rate_pct,
  round(avg(initial_edge)::numeric, 2)                                      AS avg_edge,
  round(avg(clv_final)::numeric, 2)                                         AS avg_clv,
  round(sum(clv_final)::numeric, 2)                                         AS total_clv
FROM public.ai_learning_ledger
GROUP BY 1, 2
ORDER BY 1 DESC, 2;
