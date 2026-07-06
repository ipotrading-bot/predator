-- PREDATOR PAIM v9.3 — Migration (run once in Supabase SQL Editor)
--
-- ⚠️  DO NOT RUN THIS until SUPABASE_SERVICE_KEY is set as a GitHub secret
-- AND every workflow that writes to `signals`/`ai_learning_ledger` has
-- been confirmed to actually pick it up (run_engine.py, core/audit_engine.py,
-- backfill_ledger.py now prefer SUPABASE_SERVICE_KEY, falling back to
-- SUPABASE_KEY only if the service key isn't set). Applying this migration
-- before that secret exists will break every scheduled scan/audit/backfill,
-- since they'd still be using the anon key for writes the policies below
-- no longer allow.
--
-- Context: .env.example documents SUPABASE_KEY as the anon/public
-- "read-only from the frontend" key and SUPABASE_SERVICE_KEY as the
-- service_role key meant for backend writes. In practice, every backend
-- write path used SUPABASE_KEY (the anon key) — which only worked because
-- migrate_v7_2.sql and setup_signals.sql granted `allow_all_insert` /
-- `allow_all_delete` with USING(true)/WITH CHECK(true), i.e. no RLS
-- protection at all for the anon key on `signals` and `ai_learning_ledger`.
-- The service_role key bypasses RLS entirely regardless of policy
-- content, so once the backend genuinely uses it, the anon key's write
-- access can — and should — be revoked: today it's a blank check to
-- anyone who ever obtains that "meant to be public" key.
--
-- This does NOT touch `meta` (scan heartbeat/thresholds/scan_request) —
-- api/index.py's dashboard routes write there directly with the anon
-- key by design (e.g. the "Scan" button), and that table holds no
-- betting data worth protecting the same way.

-- ============================================================
-- 1. signals — remove anon write access, keep anon read access
-- ============================================================
DROP POLICY IF EXISTS "allow_all_insert" ON public.signals;
DROP POLICY IF EXISTS "allow_all_delete" ON public.signals;

CREATE POLICY "service_role_insert" ON public.signals
  FOR INSERT TO service_role WITH CHECK (true);
CREATE POLICY "service_role_delete" ON public.signals
  FOR DELETE TO service_role USING (true);
CREATE POLICY "service_role_update" ON public.signals
  FOR UPDATE TO service_role USING (true) WITH CHECK (true);

-- "allow_all_read" (SELECT, anon-friendly) is left untouched — the
-- dashboard/ledger/audit/performance pages need it.

-- ============================================================
-- 2. ai_learning_ledger — same treatment
-- ============================================================
DROP POLICY IF EXISTS "ledger_insert" ON public.ai_learning_ledger;

CREATE POLICY "service_role_insert" ON public.ai_learning_ledger
  FOR INSERT TO service_role WITH CHECK (true);
CREATE POLICY "service_role_update" ON public.ai_learning_ledger
  FOR UPDATE TO service_role USING (true) WITH CHECK (true);
CREATE POLICY "service_role_delete" ON public.ai_learning_ledger
  FOR DELETE TO service_role USING (true);

-- "ledger_read" (SELECT, anon-friendly) is left untouched — /performance
-- reads this table directly.

-- ============================================================
-- 3. Post-migration checks (run manually, not part of the migration)
-- ============================================================
-- Confirm the anon key can no longer write:
--   SELECT * FROM pg_policies WHERE tablename IN ('signals', 'ai_learning_ledger');
-- Then trigger each workflow once (workflow_dispatch) and confirm signals
-- still get written/purged/settled/backfilled before relying on this in
-- production.
