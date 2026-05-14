-- PREDATOR PAIM v7.5 — Migration (run once in Supabase SQL Editor)
-- Adds: meta table for engine heartbeat (last_scan, match_count, signal_count)

CREATE TABLE IF NOT EXISTS public.meta (
  key        text PRIMARY KEY,
  value      text,
  updated_at timestamptz DEFAULT now()
);

ALTER TABLE public.meta ENABLE ROW LEVEL SECURITY;

CREATE POLICY "meta_read"   ON public.meta FOR SELECT USING (true);
CREATE POLICY "meta_insert" ON public.meta FOR INSERT WITH CHECK (true);
CREATE POLICY "meta_update" ON public.meta FOR UPDATE USING (true);
CREATE POLICY "meta_delete" ON public.meta FOR DELETE USING (true);
