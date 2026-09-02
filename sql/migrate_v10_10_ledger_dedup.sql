-- ============================================================================
-- migrate_v10_10_ledger_dedup.sql — JUMEAUX INTER-SOURCES DU LEDGER (2026-09-02)
--
-- ARCHIVER LES LIGNES JUMELLES : le même match réel réglé DEUX FOIS sous deux
-- `signal_id` différents.
--
-- OBJECTIF
-- --------
-- Un même match arrivant par deux sources porte deux `match_id` différents
-- (uuid OddsAPI d'un côté, id dérivé des noms d'équipes de l'autre) : deux
-- signaux jumeaux coexistent, se règlent chacun, et écrivent chacun leur ligne
-- de ledger. `ledger_signal_id_uniq` (migrate_v10_8) ne voit rien — les
-- `signal_id` SONT distincts, c'est le match réel qui ne l'est pas.
--
-- CONTEXTE MESURÉ (en base, 2026-09-02)
-- -------------------------------------
--   · 47 paires EXACTES : même `match`, même `selection`, même `market_type`,
--     créées à moins de 6 jours d'écart ;
--   · 7 paires FLOUES vérifiées UNE À UNE (libellés différents entre sources) ;
--   · total : 54 lignes à archiver sur ~540 vivantes (~10 %).
-- Ces doublons gonflent le n de la couche d'apprentissage — intervalles de
-- Wilson resserrés SANS information nouvelle, la façon la plus discrète de se
-- convaincre qu'on a prouvé quelque chose (même mécanique que v10_8) — et
-- faussent l'historique /performance.
--
-- Une garde code vient d'être posée (`core/db.py::_ledger_jumeau_reel`, tests
-- `tests/test_ledger_jumeaux.py`) : elle empêche les NOUVEAUX jumeaux exacts à
-- l'écriture. Cette migration nettoie le STOCK ; les deux se complètent, le
-- code tient la règle pour l'avenir, le SQL répare le passé.
--
-- CE QUE LA VÉRIFICATION A RÉVÉLÉ (dépôt, 2026-09-02) — portée élargie d'un
-- cran : AUCUNE migration n'a jamais activé la RLS sur
-- `ai_learning_ledger_archive` (créée nue par archive_retired_sports.sql puis
-- v10_5, avant que v10_9 ne fasse de la RLS systématique la règle). Comme ce
-- script touche la table, il la referme au passage — motif v9_3/v10_9 :
-- écriture réservée à `service_role`, `anon` sans accès (l'archive n'est lue
-- par aucune page du dashboard).
--
-- RÈGLE A — JUMEAUX EXACTS, générique, RECALCULÉE À L'APPLICATION
-- ----------------------------------------------------------------
-- Partition par (`match`, `selection`, `market_type`) sur les lignes
-- `created_at >= '2026-08-01'` (tout l'antérieur est déjà parti en archive
-- par v10_5) ; on garde le rang 1, classé « LE DÉCISIF GAGNE puis LE PLUS
-- ANCIEN » :
--
--     ORDER BY (outcome IN ('WIN','LOSS','PUSH')) DESC, created_at ASC,
--              signal_id ASC
--
-- Pourquoi cet ordre et pas « le plus vieux » tout court : la paire
-- Hellas Syrou vs GS Marko a une ligne PUSH et une ligne expired — c'est la
-- PUSH (le résultat réel) qu'on garde, pas la plus vieille. Même parti pris
-- que le dédoublonnage de v10_8 et que `_ledger_deja_ecrit`.
--
-- Au 2026-09-02 la règle rend 47 lignes. Recalculée à l'application, elle
-- attrapera AUSSI les jumeaux nés entre-temps : un décompte supérieur à 47
-- n'est pas une anomalie, c'est voulu. LIMITE CONNUE, à garder en tête si
-- l'application tarde : la règle ne distingue pas un jumeau inter-sources
-- d'une VRAIE revanche (deux équipes se rencontrent deux fois par saison —
-- la mise en garde de v10_8 contre toute contrainte sur ce triplet). Les 47
-- paires du 2026-09-02 ont été inspectées ; si le décompte a bougé, passer la
-- requête « AVANT APPLICATION » ci-dessous et REGARDER les paires nouvelles.
-- C'est précisément parce qu'on ARCHIVE (récupérable, bloc RESTAURATION) au
-- lieu de supprimer que cette règle générique est acceptable là où la
-- contrainte unique de v10_8 ne l'était pas.
--
-- RÈGLE B — JUMEAUX FLOUS, liste d'ids MORTE, épinglés un à un
-- -------------------------------------------------------------
-- 7 lignes aux libellés différents entre sources, vérifiées à la main le
-- 2026-09-02. L'appariement flou automatique a été essayé le même jour et
-- REJETÉ : il confondait U23/U19 avec les seniors et « Atletico Junior
-- Barranquilla » avec « Atletico Nacional ». Donc AUCUNE règle floue en SQL —
-- une liste d'ids figée, rien d'autre. (Les jumeaux à libellés différents
-- relèvent du pont d'alias, pas d'une devinette SQL.)
--
-- CLUSTER PACHUCA — traité PARTIELLEMENT, à dessein
-- --------------------------------------------------
-- Les deux paires exactes (« CF Pachuca vs CD Guadalajara » sid 9560/9715 ;
-- « Pachuca (W) vs Chivas Guadalajara (W) » sid 9555/9714) tombent par la
-- règle A, mais on GARDE une ligne de chaque libellé : impossible de trancher
-- sur pièces si « CF Pachuca vs CD Guadalajara » désigne le match féminin
-- (même vrai match) ou un match masculin distinct joué le même jour. On ne
-- devine pas : deux prétendants → refus, comme au settlement.
--
-- Les vues `monthly_performance`, `performance_summary` et `win_rate_monthly`
-- dérivent de `ai_learning_ledger` : elles se mettent à jour toutes seules,
-- il n'y a rien à y faire (comme v10_5).
--
-- Ce script ne DÉTRUIT rien (règle dure n°9) : il DÉPLACE vers
-- `ai_learning_ledger_archive` puis retire de la table vivante les seules
-- lignes copiées, dans la MÊME transaction — motif de migrate_v10_5.
-- Idempotent : rejoué, il ne trouve plus de rang > 1 et la liste B est déjà
-- partie ; une ligne déjà archivée n'est pas dupliquée (clé primaire
-- conservée, ON CONFLICT DO NOTHING).
--
-- À APPLIQUER À LA MAIN dans le SQL Editor Supabase — aucun runner ne
-- l'exécutera.
-- ============================================================================

-- ── 0. AVANT APPLICATION (facultatif mais recommandé si on n'est plus le
--       2026-09-02) : voir ce que la règle A va archiver, et REGARDER les
--       paires apparues depuis la mesure — une vraie revanche n'est pas un
--       jumeau. À exécuter seule, avant le reste :
--
--   SELECT match, selection, market_type, outcome, created_at, signal_id
--   FROM public.ai_learning_ledger
--   WHERE id IN (
--     SELECT id FROM (
--       SELECT id, row_number() OVER (
--                PARTITION BY match, selection, market_type
--                ORDER BY (outcome IN ('WIN','LOSS','PUSH')) DESC,
--                         created_at ASC, signal_id ASC) AS rang
--       FROM public.ai_learning_ledger
--       WHERE created_at >= '2026-08-01T00:00:00Z') r
--     WHERE rang > 1)
--   ORDER BY match, created_at;
--   -- attendu au 2026-09-02 : 47 lignes

BEGIN;

-- ── 1. Table d'archive — déjà créée par v10_5, ceci est idempotent ─────────
CREATE TABLE IF NOT EXISTS ai_learning_ledger_archive
    (LIKE ai_learning_ledger INCLUDING ALL);
ALTER TABLE ai_learning_ledger_archive
    ADD COLUMN IF NOT EXISTS archived_at timestamptz NOT NULL DEFAULT now();

-- RLS jamais posée sur l'archive (cf. en-tête) : on la referme en la
-- touchant. `service_role` porte `rolbypassrls`, l'opérateur passe par le
-- SQL Editor (propriétaire) — personne d'utile n'est gêné. Motif v10_9 :
-- REVOKE en plus de la RLS, pour qu'un futur `CREATE POLICY … TO PUBLIC`
-- ne suffise pas à tout rouvrir.
ALTER TABLE ai_learning_ledger_archive ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON ai_learning_ledger_archive FROM anon, authenticated;

-- ── 2. Copie vers l'archive : règle A (générique) ∪ règle B (ids épinglés) ─
WITH jumeaux_exacts AS (
    -- Règle A — le rang 1 (décisif, puis plus ancien) reste vivant.
    SELECT id,
           row_number() OVER (
               PARTITION BY match, selection, market_type
               ORDER BY (outcome IN ('WIN','LOSS','PUSH')) DESC,
                        created_at ASC, signal_id ASC
           ) AS rang
    FROM ai_learning_ledger
    WHERE created_at >= '2026-08-01T00:00:00Z'
)
INSERT INTO ai_learning_ledger_archive
SELECT l.*, now() AS archived_at
FROM ai_learning_ledger l
WHERE l.id IN (SELECT id FROM jumeaux_exacts WHERE rang > 1)
   OR l.id IN (
        -- Règle B — jumeaux flous, vérifiés un à un le 2026-09-02.
        '0a917726-e05c-47bf-b32e-dd7b32f537ce',  -- Tepatitlan de Morelos vs Tlaxcala (sid 9360) : jumeau de « Tepatitlan FC vs Tlaxcala FC » (sid 9372, gardé)
        '3c8ea9c9-8413-44b8-82e4-ee073ab66a28',  -- Angel City FC (W) vs Gotham FC (W) (sid 9567) : jumeau de « Angel City W vs NJ/NY Gotham FC W » (sid 9535, gardé)
        '847ced9b-67c1-4f20-8c6a-56dde422c01c',  -- San Luis de Quillota vs Santiago Wanderers (sid 9656) : jumeau de « San Luis vs Santiago Wanderers » (sid 9601, gardé)
        '3efad365-7bfd-40d0-8399-883dc3cf1d7f',  -- Athletic Club Sjdr MG vs Gremio Novorizontino SP (sid 9654) : jumeau de « Athletic Club vs Novorizontino » (sid 9639, gardé)
        'cdde5e6b-2488-4997-a728-dc59d0c7217a',  -- Jeju United FC vs Pohang Steelers (sid 9678) : jumeau de « Jeju SK FC vs FC Pohang Steelers » (sid 9655, gardé)
        '5a34ffe7-ef26-4301-a024-662d0426151a',  -- CA Excursionistas vs Argentino de Merlo (sid 9726) : jumeau de « Excursionistas vs Argentino de Merlo » (sid 9668, gardé — deux expired, se résorberont ensemble)
        'ef7eb3b5-6e00-48e2-b01c-155423702180'   -- CA Acassuso vs CA San Telmo (sid 9766) : jumeau de « Acassuso vs San Telmo » (sid 9719, gardé)
   )
ON CONFLICT DO NOTHING;

-- ── 3. Retrait — UNIQUEMENT les lignes effectivement copiées ───────────────
DELETE FROM ai_learning_ledger l
USING ai_learning_ledger_archive a
WHERE l.id = a.id;

COMMIT;

-- ── RESTAURATION (si une paire s'avère être une vraie revanche, pas un
--    jumeau — la re-verser recrée sciemment le doublon, donc UNIQUEMENT
--    après vérification sur pièces) ────────────────────────────────────────
--   BEGIN;
--   INSERT INTO ai_learning_ledger
--   SELECT <colonnes sauf archived_at> FROM ai_learning_ledger_archive
--    WHERE id = '<uuid>'          -- ou : archived_at::date = '2026-09-02'
--   ON CONFLICT DO NOTHING;
--   DELETE FROM ai_learning_ledger_archive
--    WHERE id = '<uuid>';         -- même critère que la copie
--   COMMIT;
--   -- `ledger_signal_id_uniq` ne gênera pas : les jumeaux ont des signal_id
--   -- distincts, c'est tout le problème. Mais `_ledger_jumeau_reel`
--   -- (core/db.py) refuserait de RÉÉCRIRE une telle ligne : la restauration
--   -- SQL est le seul chemin de retour, c'est voulu.

-- ── SELECT TÉMOINS après application ───────────────────────────────────────
-- (1) Ce qui est parti en archive aujourd'hui :
--   SELECT count(*) FROM ai_learning_ledger_archive
--    WHERE archived_at::date = current_date;
--   -- attendu : 54 au 2026-09-02 (47 exacts + 7 flous) ; davantage si des
--   -- jumeaux sont nés entre la mesure et l'application — voulu, cf. en-tête.
--
-- (2) La règle A ne doit plus rien trouver :
--   SELECT match, selection, market_type, count(*) AS n
--   FROM ai_learning_ledger
--   WHERE created_at >= '2026-08-01T00:00:00Z'
--   GROUP BY 1, 2, 3
--   HAVING count(*) > 1;
--   -- attendu : 0 ligne
--
-- (3) Décompte vivant :
--   SELECT count(*) FROM ai_learning_ledger;
--   -- attendu : ≈ 486 (540 − 54 au moment de la mesure du 2026-09-02)
--
-- (4) L'archive est bien refermée :
--   SELECT has_table_privilege('anon', 'ai_learning_ledger_archive', 'INSERT');
--   -- attendu : false
--
-- RAPPEL OPÉRATEUR : à appliquer à la main dans le SQL Editor Supabase —
-- aucun runner ne l'exécutera. La preuve d'application est le témoin (2) :
-- 0 ligne.
