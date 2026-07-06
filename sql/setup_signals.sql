-- PREDATOR PAIM v7.0 — Full schema reset
-- CASCADE drops dependent views (performance_summary, monthly_performance)
--
-- ⚠️  ONE-TIME BOOTSTRAP ONLY — DO NOT RE-RUN ON A LIVE DATABASE.
-- Unlike every migrate_v*.sql in this folder (all idempotent, ADD COLUMN
-- IF NOT EXISTS), this script starts with DROP TABLE ... CASCADE and will
-- irreversibly delete every row in `signals` (and any dependent view) if
-- executed again. It exists only to recreate the schema from scratch in a
-- brand-new Supabase project. If `signals` already has data you care
-- about, do not paste this file into the SQL Editor — use the migrate_v*
-- files instead, in order.

DROP TABLE IF EXISTS public.signals CASCADE;

CREATE TABLE public.signals (
  id             bigserial PRIMARY KEY,
  created_at     timestamptz DEFAULT now(),
  scanned_at     timestamptz,
  match          text        NOT NULL,
  league         text,
  sport          text        DEFAULT 'football',
  xbet_odd       numeric(6,2),
  pinnacle_price numeric(6,2),
  edge_pct       numeric(6,2),
  risk_flag      text,
  status         text        DEFAULT 'active'
);

-- Recreate performance views on new schema
CREATE OR REPLACE VIEW public.performance_summary AS
SELECT
  risk_flag,
  count(*)                        AS total,
  round(avg(edge_pct)::numeric, 2) AS avg_edge,
  round(max(edge_pct)::numeric, 2) AS best_edge,
  count(*) FILTER (WHERE edge_pct >= 8) AS high_value,
  count(*) FILTER (WHERE edge_pct >= 3) AS value_bets
FROM public.signals
GROUP BY risk_flag;

CREATE OR REPLACE VIEW public.monthly_performance AS
SELECT
  date_trunc('month', created_at)  AS month,
  count(*)                          AS total_signals,
  round(avg(edge_pct)::numeric, 2)  AS avg_edge,
  count(*) FILTER (WHERE edge_pct >= 3) AS value_bets
FROM public.signals
GROUP BY 1
ORDER BY 1 DESC;

-- RLS: allow anon key to read and insert
ALTER TABLE public.signals ENABLE ROW LEVEL SECURITY;

CREATE POLICY "allow_all_read"   ON public.signals FOR SELECT USING (true);
CREATE POLICY "allow_all_insert" ON public.signals FOR INSERT WITH CHECK (true);
