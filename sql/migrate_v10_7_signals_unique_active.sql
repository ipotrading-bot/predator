-- ============================================================================
-- migrate_v10_7_signals_unique_active.sql — PHASE B2 (2026-08-27)
--
-- UN SEUL SIGNAL ACTIF PAR (match_id, market_key), garanti par la BASE.
--
-- POURQUOI
-- --------
-- `run_engine._save` faisait un SELECT puis, selon le résultat, un UPDATE ou
-- un INSERT. Entre les deux, rien ne tient : deux runs qui se chevauchent —
-- et ils se chevauchent, le scan standard et le tick golden partagent des
-- ligues — lisent tous les deux « aucune ligne », puis insèrent tous les deux.
-- Le dashboard affiche alors le même pari deux fois, `_portfolio_balance` le
-- compte deux fois dans son quota par sport, et `risk_manager` double son
-- exposition sans le savoir. Aucune erreur n'est levée : c'est un doublon
-- parfaitement valide du point de vue du schéma actuel.
--
-- Un index ne se contourne pas. Le code peut se tromper, se faire tuer au
-- milieu, ou tourner en deux exemplaires ; la contrainte tient quand même.
--
-- ⚠️ POURQUOI L'INDEX EST PARTIEL, ET POURQUOI CE N'EST PAS UN DÉTAIL
-- -------------------------------------------------------------------
--   status = 'active'
--     Une ligne réglée, close ou expirée doit pouvoir COHABITER avec un
--     nouveau signal actif sur le même match et le même marché : c'est le
--     comportement voulu de `_save` (« le scope status='active' garantit
--     qu'une ligne settled/closed/expired n'est JAMAIS ressuscitée : on
--     insère une ligne neuve à côté »). Un index total l'interdirait et ferait
--     échouer tout nouveau scan d'un match déjà réglé.
--     Un index sur (match_id, market_key, status) serait pire encore : deux
--     signaux successifs sur le même match peuvent parfaitement finir tous les
--     deux en 'expired', et la seconde expiration échouerait alors en silence.
--
--   match_id <> '' AND market_key <> ''
--     `run_engine._emit` écrit `match_id` avec un DÉFAUT VIDE (`""`) quand la
--     source ne fournit pas d'identifiant. Sans cette clause, DEUX MATCHS
--     DIFFÉRENTS dépourvus d'id entreraient en conflit sur ('', 'h2h'), et le
--     code, croyant retrouver « sa » ligne, écraserait le signal d'un autre
--     match. On ne contraint donc que les lignes réellement identifiables.
--     Mesuré le 2026-08-27 : 90 lignes actives, 0 sans `match_id`. La clause
--     est un filet, pas une réponse à un cas courant.
--
-- ⚠️ CE QUE CET INDEX N'AUTORISE PAS : `upsert(on_conflict=…)` de PostgREST.
-- PostgreSQL n'infère un index unique PARTIEL comme cible de `ON CONFLICT`
-- que si l'ordre porte lui-même le prédicat (`ON CONFLICT (cols) WHERE …`).
-- PostgREST n'expose aucun moyen de le transmettre : `upsert(on_conflict=…)`
-- ne prend que des noms de colonnes. Un upsert sur cet index échouerait donc
-- en 42P10, « no unique or exclusion constraint matching the ON CONFLICT
-- specification ». C'est pourquoi `_save` fait INSERT puis, sur violation
-- d'unicité (23505), un UPDATE ciblé — même effet, même absence de course, et
-- c'est la BASE qui arbitre au lieu d'une lecture antérieure.
--
-- À APPLIQUER À LA MAIN dans le SQL Editor Supabase. Idempotent.
-- ============================================================================

-- ── 1. Vérifier d'abord. Ne pas exécuter l'étape 2 à l'aveugle ─────────────
-- Si cette requête rend des lignes, l'étape 3 échouera. Regarder ce qu'elle
-- rend AVANT de dédoublonner : un doublon peut révéler un bug d'appariement
-- de noms, auquel cas supprimer les lignes cacherait la cause.
SELECT match_id, market_key, count(*) AS n, array_agg(id ORDER BY created_at)
FROM public.signals
WHERE status = 'active'
  AND match_id IS NOT NULL AND match_id <> ''
  AND market_key IS NOT NULL AND market_key <> ''
GROUP BY match_id, market_key
HAVING count(*) > 1;

-- ── 2. Dédoublonnage — À N'EXÉCUTER QUE SI L'ÉTAPE 1 A RENDU DES LIGNES ────
-- On garde la ligne la PLUS ANCIENNE de chaque groupe, pas la plus récente :
-- c'est elle qui porte le `created_at` d'origine (donc le vrai
-- time-to-match), et c'est son `id` que `ai_learning_ledger` a pu recopier.
-- Les doublons plus jeunes n'ont, eux, aucune référence entrante.
--
-- DELETE FROM public.signals s
-- USING (
--   SELECT match_id, market_key, min(created_at) AS garde
--   FROM public.signals
--   WHERE status = 'active'
--     AND match_id IS NOT NULL AND match_id <> ''
--     AND market_key IS NOT NULL AND market_key <> ''
--   GROUP BY match_id, market_key
--   HAVING count(*) > 1
-- ) d
-- WHERE s.status = 'active'
--   AND s.match_id = d.match_id
--   AND s.market_key = d.market_key
--   AND s.created_at > d.garde;

-- ── 3. La contrainte ───────────────────────────────────────────────────────
CREATE UNIQUE INDEX IF NOT EXISTS signals_active_match_market_uniq
  ON public.signals (match_id, market_key)
  WHERE status = 'active'
    AND match_id IS NOT NULL AND match_id <> ''
    AND market_key IS NOT NULL AND market_key <> '';

-- ── 4. Vérification après application ──────────────────────────────────────
-- Doit rendre exactement une ligne :
--   SELECT indexname, indexdef FROM pg_indexes
--   WHERE tablename = 'signals' AND indexname = 'signals_active_match_market_uniq';
--
-- Et le moteur doit continuer à écrire. Après le premier scan :
--   SELECT count(*) FROM public.signals WHERE status = 'active';
--
-- ── Retour arrière ─────────────────────────────────────────────────────────
--   DROP INDEX IF EXISTS public.signals_active_match_market_uniq;
-- Le code fonctionne sans l'index : `_save` retombe alors sur le chemin
-- d'INSERT simple, sans violation d'unicité à intercepter. Il perd seulement
-- la garantie, pas la capacité d'écrire.
