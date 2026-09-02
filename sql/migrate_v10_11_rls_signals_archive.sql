-- ============================================================================
-- migrate_v10_11_rls_signals_archive.sql — RLS SUR `signals_archive` (2026-09-02)
--
-- REFERMER LA DERNIÈRE ARCHIVE NUE : `signals_archive` n'a jamais eu de RLS.
--
-- OBJECTIF
-- --------
-- Trouvée NUE pendant la rédaction de v10_10 : `signals_archive` a été créée
-- par `migrate_v10_5_archive_pre_august.sql` (CREATE TABLE ... LIKE signals,
-- lignes 90-93) SANS activer la RLS — v10_5 est antérieure à v10_9, qui a
-- fait de la RLS systématique la règle. v10_10 a refermé
-- `ai_learning_ledger_archive` au passage parce qu'elle la touchait ;
-- `signals_archive`, elle, n'était pas touchée et restait ouverte. Cette
-- migration lui applique le MÊME traitement (motif v9_3/v10_9) :
-- ENABLE ROW LEVEL SECURITY + REVOKE sur `anon`/`authenticated`.
--
-- CONTEXTE VÉRIFIÉ (dépôt, 2026-09-02)
-- ------------------------------------
--   · `sql/` entier passé au grep : la SEULE migration qui mentionne
--     `signals_archive` est v10_5 (création + copie), aucune ne lui a posé
--     de RLS ni de REVOKE ;
--   · hors `sql/`, AUCUN code ne lit `signals_archive` — zéro occurrence
--     dans api/, core/, templates/ (seules AUDIT.md et INCIDENTS.md la
--     citent) : l'archive n'est lue par aucune page du dashboard, refermer
--     la lecture anon ne casse rien ;
--   · `service_role` porte `rolbypassrls` (vérifié en base pour v10_9) et
--     l'opérateur passe par le SQL Editor (propriétaire) — personne d'utile
--     n'est gêné, ni pour archiver ni pour restaurer.
--
-- Motif v10_9/v10_10 : le REVOKE s'ajoute à la RLS pour qu'un futur
-- `CREATE POLICY … TO PUBLIC` ne suffise pas à tout rouvrir.
--
-- Idempotent : ENABLE ROW LEVEL SECURITY et REVOKE sont sans effet s'ils
-- sont rejoués. Aucune ligne n'est touchée (règle dure n°9 : rien à
-- archiver ici, on ne fait que fermer des droits).
--
-- À APPLIQUER À LA MAIN dans le SQL Editor Supabase — aucun runner ne
-- l'exécutera.
-- ============================================================================

BEGIN;

-- RLS activée sans aucune policy : par défaut, tout est refusé aux rôles
-- soumis à la RLS. `service_role` bypasse (rolbypassrls), le propriétaire
-- (SQL Editor) aussi.
ALTER TABLE public.signals_archive ENABLE ROW LEVEL SECURITY;

-- Ceinture ET bretelles : les GRANT de table hérités de la création
-- (LIKE ne copie pas les GRANT, mais le schéma public en distribue par
-- défaut aux rôles Supabase) sont retirés à `anon` et `authenticated`.
REVOKE ALL ON public.signals_archive FROM anon, authenticated;

COMMIT;

-- ── RESTAURATION (si un lecteur légitime de l'archive apparaît un jour,
--    p. ex. une page d'historique du dashboard) ─────────────────────────────
--   BEGIN;
--   GRANT SELECT ON public.signals_archive TO anon;
--   CREATE POLICY "archive_read" ON public.signals_archive
--     FOR SELECT TO anon USING (true);
--   COMMIT;
--   -- Lecture seule uniquement — l'écriture reste à `service_role`, comme
--   -- partout depuis v9_3.

-- ── SELECT TÉMOIN après application ────────────────────────────────────────
--   SELECT has_table_privilege('anon', 'signals_archive', 'INSERT');
--   -- attendu : false
--
-- RAPPEL OPÉRATEUR : à appliquer à la main dans le SQL Editor Supabase —
-- aucun runner ne l'exécutera. La preuve d'application est le témoin
-- ci-dessus : false.
