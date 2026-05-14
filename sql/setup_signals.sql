-- PREDATOR PAIM v7.0 — Signals table setup
-- Run this in Supabase Dashboard > SQL Editor

-- Drop old table and recreate with full schema
DROP TABLE IF EXISTS signals;

CREATE TABLE signals (
  id            bigserial PRIMARY KEY,
  created_at    timestamptz DEFAULT now(),
  scanned_at    timestamptz,
  match         text        NOT NULL,
  league        text,
  sport         text        DEFAULT 'football',
  xbet_odd      numeric(6,2),
  pinnacle_price numeric(6,2),
  edge_pct      numeric(6,2),
  risk_flag     text,
  status        text        DEFAULT 'active'
);

-- Allow anon key to read and insert (no auth needed for the engine)
ALTER TABLE signals ENABLE ROW LEVEL SECURITY;

CREATE POLICY "allow_all_read"   ON signals FOR SELECT USING (true);
CREATE POLICY "allow_all_insert" ON signals FOR INSERT WITH CHECK (true);
