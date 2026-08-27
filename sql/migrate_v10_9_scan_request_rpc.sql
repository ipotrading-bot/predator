-- ============================================================================
-- migrate_v10_9_scan_request_rpc.sql — PHASE C3 (2026-08-27)
--
-- DEMANDER UN SCAN SANS CLÉ D'ÉCRITURE : fonction `security definer`,
-- limitée en débit DU CÔTÉ SQL, appelable avec la clé anon.
--
-- OBJECTIF : retirer `SUPABASE_SERVICE_KEY` du déploiement Vercel public.
-- `/api/scan` est la SEULE écriture du dashboard (vérifié : une occurrence de
-- `_db(write=True)` dans api/index.py, ligne 1001). Tant qu'elle exige la clé
-- service_role, une faille de la fonction Vercel donne les pleins pouvoirs sur
-- `signals`, `ai_learning_ledger`, `meta` et `app_secrets` — c'est-à-dire sur
-- tout. Après cette migration, le dashboard n'a plus besoin que de lire.
--
-- ⚠️ CE QUE LA VÉRIFICATION A RÉVÉLÉ, ET QUI CHANGE LA PORTÉE
-- -----------------------------------------------------------
-- `migrate_v9_3_tighten_rls.sql` avait resserré `signals` et
-- `ai_learning_ledger` : le rôle `anon` y garde ses GRANT de table, mais les
-- policies RLS réservent l'écriture à `service_role`. `meta` a été OUBLIÉ.
-- Mesuré en base le 2026-08-27 :
--
--   meta : RLS activée, mais policies meta_insert / meta_update / meta_delete
--          accordées à PUBLIC avec la condition `true`
--   anon : GRANT INSERT, UPDATE, DELETE, TRUNCATE sur meta
--
-- Autrement dit, la clé anon pouvait déjà réécrire N'IMPORTE QUELLE clé de
-- `meta` — et `meta` porte les seuils appris (`threshold_*`), les verdicts par
-- sport, les compteurs de quota, les caches de slate lus par le mode REPRICE,
-- et les horodatages de coupe-circuit. Poser un scan par une fonction
-- verrouillée pendant que la table reste ouverte en écriture serait du
-- théâtre : la partie 3 referme cette porte, sans quoi les parties 1 et 2 ne
-- protègent rien.
--
-- La clé anon n'est PAS servie au navigateur (vérifié sur la page publique :
-- aucune trace de clé). Le risque n'est donc pas « n'importe qui » mais
-- « quiconque obtient la clé anon » — or tout le modèle Supabase, et ce dépôt
-- lui-même depuis v9_3, traite cette clé comme non fiable.
--
-- ⚠️ AUCUN ÉCRIVAIN DE `meta` N'EST CASSÉ : vérifié un par un — run_engine,
-- audit_engine, learning_layer, risk_manager, daily_quota, ai_router,
-- ai_search, free_sources, source_adapter, team_aliases passent TOUS par
-- `core.db.get_db(write=True)`, donc par la clé service_role, laquelle porte
-- `rolbypassrls = true` (vérifié en base). La lecture reste ouverte : le
-- dashboard lit `meta` avec la clé anon.
--
-- À APPLIQUER À LA MAIN dans le SQL Editor Supabase. Idempotent.
-- ============================================================================

-- ── 1. Journal des demandes — support de la limite de débit ───────────────
-- Une table plutôt qu'un compteur dans `meta` : il faut l'HORODATAGE de
-- chaque demande pour tenir une fenêtre glissante, et une ligne par demande
-- se purge trivialement. Un compteur, lui, ne saurait pas quand oublier.
CREATE TABLE IF NOT EXISTS public.scan_request_log (
    id  bigserial   PRIMARY KEY,
    ip  text        NOT NULL,
    at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS scan_request_log_at_idx    ON public.scan_request_log (at DESC);
CREATE INDEX IF NOT EXISTS scan_request_log_ip_at_idx ON public.scan_request_log (ip, at DESC);

-- Personne n'y touche directement. RLS activée SANS aucune policy : tout
-- accès direct est refusé, y compris en lecture. Seule la fonction
-- ci-dessous — qui s'exécute avec les droits de son PROPRIÉTAIRE — y écrit.
ALTER TABLE public.scan_request_log ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.scan_request_log FROM anon, authenticated;

-- ── 2. La fonction ────────────────────────────────────────────────────────
-- `SECURITY DEFINER` : elle s'exécute avec les droits du propriétaire, donc
-- elle peut écrire `meta` alors que l'appelant (anon) ne le peut pas.
--
-- `SET search_path = public, pg_temp` n'est PAS décoratif : sans lui, un
-- appelant capable de créer un objet dans un schéma temporaire pourrait faire
-- résoudre `meta` ou `now()` vers le sien et détourner une fonction qui tourne
-- en droits élevés. C'est la faute classique des fonctions `security definer`.
CREATE OR REPLACE FUNCTION public.demander_scan(p_ip text DEFAULT NULL)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    -- Limites. Elles vivent ICI et non côté application : c'est tout l'objet
    -- de C3. Un compteur en mémoire de fonction Vercel repart à zéro à chaque
    -- instance froide et ne se partage pas entre instances ; celui-ci est
    -- unique, partagé, et l'appelant ne peut pas le contourner.
    c_par_ip_n      constant int      := 3;
    c_fenetre       constant interval := interval '5 minutes';
    c_global_n      constant int      := 20;   -- garde-fou anti-IP-forgée
    c_cooldown      constant interval := interval '120 seconds';
    c_retention     constant interval := interval '1 hour';

    v_ip        text;
    v_n_ip      int;
    v_n_global  int;
    v_en_attente timestamptz;
BEGIN
    -- L'IP vient de l'appelant, donc elle est FALSIFIABLE. C'est précisément
    -- pourquoi la limite par IP est doublée d'une limite GLOBALE : forger son
    -- IP donne un compteur par IP neuf, jamais un compteur global neuf.
    v_ip := left(coalesce(nullif(btrim(p_ip), ''), 'inconnue'), 64);

    DELETE FROM public.scan_request_log WHERE at < now() - c_retention;

    SELECT count(*) INTO v_n_ip
      FROM public.scan_request_log
     WHERE ip = v_ip AND at > now() - c_fenetre;
    IF v_n_ip >= c_par_ip_n THEN
        RETURN jsonb_build_object(
            'status',  'rate_limited',
            'message', 'Trop de demandes — réessayez dans quelques minutes');
    END IF;

    SELECT count(*) INTO v_n_global
      FROM public.scan_request_log
     WHERE at > now() - c_fenetre;
    IF v_n_global >= c_global_n THEN
        RETURN jsonb_build_object(
            'status',  'rate_limited',
            'message', 'Trop de demandes — réessayez dans quelques minutes');
    END IF;

    -- La demande est COMPTÉE avant d'être honorée : une demande rejetée par
    -- le cooldown ci-dessous doit tout de même peser sur le quota, sinon un
    -- appelant peut marteler gratuitement pendant les 120 s de cooldown.
    INSERT INTO public.scan_request_log (ip) VALUES (v_ip);

    SELECT (value::jsonb ->> 'requested_at')::timestamptz
      INTO v_en_attente
      FROM public.meta
     WHERE key = 'scan_request';

    IF v_en_attente IS NOT NULL AND v_en_attente > now() - c_cooldown THEN
        RETURN jsonb_build_object(
            'status',  'already_queued',
            'message', 'Un scan est déjà en attente — réessayez dans quelques minutes');
    END IF;

    INSERT INTO public.meta (key, value, updated_at)
    VALUES ('scan_request',
            jsonb_build_object('requested_at', now())::text,
            now())
    ON CONFLICT (key) DO UPDATE
       SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at;

    RETURN jsonb_build_object(
        'status',  'queued',
        'message', 'Scan demandé — résultats sous 30 min max (prochain passage planifié)');
EXCEPTION
    -- Une valeur de `meta.scan_request` illisible (JSON cassé, écrit à la
    -- main) ne doit pas rendre la demande de scan impossible pour toujours.
    WHEN others THEN
        RETURN jsonb_build_object(
            'status',  'error',
            'message', 'la demande de scan a échoué');
END;
$$;

-- `PUBLIC` inclut tout rôle présent et à venir : on retire d'abord, on
-- accorde ensuite, nommément.
REVOKE ALL ON FUNCTION public.demander_scan(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.demander_scan(text) TO anon, authenticated, service_role;

-- ── 3. Refermer `meta` en écriture — sans quoi 1 et 2 ne protègent rien ───
DROP POLICY IF EXISTS "meta_insert" ON public.meta;
DROP POLICY IF EXISTS "meta_update" ON public.meta;
DROP POLICY IF EXISTS "meta_delete" ON public.meta;

-- La LECTURE reste ouverte : le dashboard lit `meta` avec la clé anon
-- (dernier scan, seuils affichés, état des caches).
DROP POLICY IF EXISTS "meta_read" ON public.meta;
CREATE POLICY "meta_read" ON public.meta
    FOR SELECT USING (true);

-- L'écriture est réservée à service_role. Ces policies sont explicites bien
-- que `service_role` porte `rolbypassrls` : elles documentent l'intention
-- dans le schéma, et le jour où ce rôle perdrait ce privilège, rien ne
-- casserait en silence. Même parti pris qu'en v9_3.
CREATE POLICY "meta_service_insert" ON public.meta
    FOR INSERT TO service_role WITH CHECK (true);
CREATE POLICY "meta_service_update" ON public.meta
    FOR UPDATE TO service_role USING (true) WITH CHECK (true);
CREATE POLICY "meta_service_delete" ON public.meta
    FOR DELETE TO service_role USING (true);

-- Ceinture et bretelles : retirer aussi les GRANT de table. RLS suffirait,
-- mais un futur `CREATE POLICY … TO PUBLIC` rouvrirait tout d'un coup ; sans
-- le GRANT, il ne suffirait pas.
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON public.meta FROM anon, authenticated;

-- ── 4. Vérification après application ─────────────────────────────────────
-- a) la fonction existe, est `security definer`, et son search_path est fixé :
--   SELECT proname, prosecdef, proconfig FROM pg_proc
--    WHERE proname = 'demander_scan';
--   → prosecdef = true, proconfig = {search_path=public,pg_temp}
--
-- b) anon peut l'EXÉCUTER :
--   SELECT has_function_privilege('anon', 'public.demander_scan(text)', 'EXECUTE');
--   → true
--
-- c) anon ne peut PLUS écrire meta :
--   SELECT has_table_privilege('anon', 'public.meta', 'UPDATE');
--   → false
--
-- d) la lecture reste ouverte :
--   SELECT has_table_privilege('anon', 'public.meta', 'SELECT');
--   → true
--
-- ── Retour arrière ────────────────────────────────────────────────────────
--   GRANT INSERT, UPDATE, DELETE ON public.meta TO anon, authenticated;
--   DROP POLICY IF EXISTS "meta_service_insert" ON public.meta;
--   DROP POLICY IF EXISTS "meta_service_update" ON public.meta;
--   DROP POLICY IF EXISTS "meta_service_delete" ON public.meta;
--   CREATE POLICY "meta_insert" ON public.meta FOR INSERT WITH CHECK (true);
--   CREATE POLICY "meta_update" ON public.meta FOR UPDATE USING (true);
--   CREATE POLICY "meta_delete" ON public.meta FOR DELETE USING (true);
--   DROP FUNCTION IF EXISTS public.demander_scan(text);
--   DROP TABLE IF EXISTS public.scan_request_log;
-- …et REMETTRE `SUPABASE_SERVICE_KEY` sur Vercel, sinon /api/scan ne peut
-- plus rien écrire par aucun chemin.
