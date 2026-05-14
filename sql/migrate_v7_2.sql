-- PREDATOR PAIM v7.2 — Migration (run once in Supabase SQL Editor)
-- Adds: market column, DELETE RLS policy, updated views

-- 1. Add market column if missing
ALTER TABLE public.signals
  ADD COLUMN IF NOT EXISTS market text DEFAULT 'Moneyline';

-- 2. Allow service role to delete erroneous signals
CREATE POLICY "allow_all_delete" ON public.signals
  FOR DELETE USING (true);

-- 3. Refresh performance views with updated thresholds (1.5 / 2.5)
CREATE OR REPLACE VIEW public.performance_summary AS
SELECT
  sport,
  risk_flag,
  count(*)                          AS total,
  round(avg(edge_pct)::numeric, 2)  AS avg_edge,
  round(max(edge_pct)::numeric, 2)  AS best_edge,
  count(*) FILTER (WHERE edge_pct >= 2.5) AS elite_signals,
  count(*) FILTER (WHERE edge_pct >= 1.5) AS dashboard_signals
FROM public.signals
GROUP BY sport, risk_flag;

CREATE OR REPLACE VIEW public.monthly_performance AS
SELECT
  date_trunc('month', created_at)   AS month,
  sport,
  count(*)                          AS total_signals,
  round(avg(edge_pct)::numeric, 2)  AS avg_edge,
  count(*) FILTER (WHERE edge_pct >= 2.5) AS elite_bets,
  count(*) FILTER (WHERE edge_pct >= 1.5) AS dashboard_bets
FROM public.signals
GROUP BY 1, 2
ORDER BY 1 DESC;
