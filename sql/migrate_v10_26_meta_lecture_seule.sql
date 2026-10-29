-- ============================================================================
-- migrate_v10_26_meta_lecture_seule.sql — `meta` EN LECTURE SEULE POUR LES
-- RÔLES PUBLICS, COMME LES AUTRES TABLES (2026-10-29)
--
-- v10_9 avait retiré à `anon`/`authenticated` l'écriture sur `meta`, mais
-- laissé TRIGGER, REFERENCES et MAINTAIN — constaté après v10_24, qui a
-- ramené les sept autres tables à SELECT seul. TRIGGER permet de poser un
-- déclencheur sur la table, MAINTAIN de la VACUUM/REINDEX : rien de cela ne
-- revient à un rôle public. La lecture reste (dashboard, ci_scan_mode).
--
-- Aucune donnée touchée. RESTAURATION :
-- `GRANT REFERENCES, TRIGGER, MAINTAIN ON TABLE public.meta TO anon, authenticated;`
-- ============================================================================

REVOKE ALL ON TABLE public.meta FROM anon, authenticated;
GRANT SELECT ON TABLE public.meta TO anon, authenticated;

-- Vérification : aucun privilège autre que SELECT pour anon/authenticated
-- SELECT c.relname, a.grantee::regrole, a.privilege_type
--   FROM pg_class c CROSS JOIN LATERAL aclexplode(c.relacl) a
--  WHERE c.relnamespace = 'public'::regnamespace
--    AND a.grantee IN ('anon'::regrole, 'authenticated'::regrole)
--    AND a.privilege_type <> 'SELECT';   → 0 ligne
