-- ============================================================================
-- migrate_v10_24_durcissement_droits.sql — LES RÔLES PUBLICS NE GARDENT QUE
-- LA LECTURE (2026-10-29, audit général)
--
-- OBJECTIF
-- --------
-- Mesuré le 2026-10-29 (aclexplode sur pg_class) : `anon` et `authenticated`
-- détenaient encore les droits Postgres PAR DÉFAUT de Supabase sur sept tables
-- — INSERT, UPDATE, DELETE et TRUNCATE compris — sur `ai_learning_ledger`,
-- `signals`, `brier_scores`, `team_aliases`, `scan_logs`, `scanner_state` et
-- `bankroll_snapshots`. Seul `meta` avait été resserré (v10_9).
-- Aucune faille exploitable aujourd'hui : les policies RLS réservent toute
-- écriture à `service_role`, et PostgREST n'expose pas TRUNCATE. Mais la seule
-- barrière était la policy : une policy mal écrite demain (un `TO public` de
-- trop) ouvrirait l'écriture du ledger à quiconque lit la clé anon du
-- dashboard. TRUNCATE, lui, n'est soumis à AUCUNE policy RLS.
--
-- Au passage, les trois alertes du conseiller Supabase :
--   · 3 vues SECURITY DEFINER (`win_rate_monthly`, `performance_summary`,
--     `monthly_performance`) — lues par AUCUN code du dépôt, elles exposent
--     un taux de réussite nu (règle 7) et `clv_final`, la re-dérivation
--     tautologique de l'edge d'entrée (INCIDENTS « Le CLV du dashboard »).
--     Passées en security_invoker : la RLS de l'appelant s'applique.
--   · 2 fonctions héritées (`update_signal_result`, `refresh_dashboard_stats`)
--     exécutables par anon, search_path mutable, appelées par aucun code :
--     droit d'exécution retiré aux rôles publics, search_path figé.
--   `demander_scan` (bouton « Scanner » du dashboard) reste appelable par anon
--   : c'est voulu (v10_9), limité par IP et globalement, search_path figé.
--
-- Rien n'est supprimé, aucune ligne touchée. Effet fonctionnel : AUCUN (le
-- dashboard lit avec la clé anon — SELECT conservé là où une policy de lecture
-- existe ; le moteur écrit avec service_role, non concerné).
--
-- RESTAURATION : `GRANT ALL ON TABLE <t> TO anon, authenticated;` par table ;
-- `ALTER VIEW <v> SET (security_invoker = false);` ;
-- `GRANT EXECUTE ON FUNCTION <f> TO anon, authenticated;`.
-- ============================================================================

-- 1. Tables : tout retirer, rendre la seule LECTURE là où une policy la prévoit.
REVOKE ALL ON TABLE public.ai_learning_ledger, public.signals, public.brier_scores,
                    public.team_aliases, public.scan_logs, public.scanner_state,
                    public.bankroll_snapshots
  FROM anon, authenticated;

GRANT SELECT ON TABLE public.ai_learning_ledger, public.signals, public.brier_scores,
                     public.team_aliases, public.scan_logs, public.bankroll_snapshots
  TO anon, authenticated;
-- scanner_state : aucune policy de lecture, rien à rendre.

-- 2. Vues : la RLS de l'appelant, plus celle du créateur.
ALTER VIEW public.win_rate_monthly    SET (security_invoker = true);
ALTER VIEW public.performance_summary SET (security_invoker = true);
ALTER VIEW public.monthly_performance SET (security_invoker = true);
REVOKE ALL ON TABLE public.win_rate_monthly, public.performance_summary,
                    public.monthly_performance FROM anon, authenticated;
GRANT SELECT ON TABLE public.win_rate_monthly, public.performance_summary,
                     public.monthly_performance TO anon, authenticated;

-- 2 bis. Séquences : `anon` pouvait les avancer (UPDATE = setval/nextval).
-- `demander_scan` (security definer) écrit scan_request_log en propriétaire.
REVOKE ALL ON SEQUENCE public.signals_id_seq, public.team_aliases_id_seq,
                       public.scan_request_log_id_seq
  FROM anon, authenticated;

-- 3. Fonctions héritées : plus exécutables par les rôles publics.
ALTER FUNCTION public.update_signal_result(uuid, smallint, numeric, numeric)
  SET search_path = public, pg_temp;
ALTER FUNCTION public.refresh_dashboard_stats() SET search_path = public, pg_temp;
REVOKE EXECUTE ON FUNCTION public.update_signal_result(uuid, smallint, numeric, numeric)
  FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.refresh_dashboard_stats() FROM PUBLIC, anon, authenticated;

-- Vérification :
-- SELECT c.relname, a.grantee::regrole, string_agg(a.privilege_type, ',')
--   FROM pg_class c CROSS JOIN LATERAL aclexplode(c.relacl) a
--  WHERE c.relnamespace = 'public'::regnamespace
--    AND a.grantee IN ('anon'::regrole, 'authenticated'::regrole)
--  GROUP BY 1, 2 ORDER BY 1;
-- → SELECT seulement (et rien sur scanner_state).
