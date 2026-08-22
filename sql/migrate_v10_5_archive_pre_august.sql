-- sql/migrate_v10_5_archive_pre_august.sql
-- ARCHIVAGE : juillet 2026 + sports retirés — « on repart d'août »
--
-- DÉCISION OPÉRATEUR (2026-08-22) : « predator n'était pas au point et avait
-- des bugs en juillet, on recommence tout en août ». Les lignes de juillet ne
-- mesurent donc pas la performance du système actuel : les garder dans les
-- agrégats revient à juger la version d'aujourd'hui sur les erreurs d'une
-- version corrigée depuis.
--
-- Ce script ne DÉTRUIT rien. Il DÉPLACE vers `ai_learning_ledger_archive`
-- (même schéma + `archived_at`) puis retire de la table vivante, dans la MÊME
-- transaction. C'est la politique déjà établie par
-- sql/archive_retired_sports.sql, et sa raison vaut ici aussi : ces lignes
-- (cotes, edge d'entrée, CLV, issue réelle) sont la seule trace empirique du
-- comportement passé. Un backtest futur qui ignorerait des paris réglés
-- souffrirait d'un biais de survie — et « juillet était buggé » est une
-- hypothèse qu'on peut vouloir re-vérifier sur pièces.
--
-- PÉRIMÈTRE (mesuré avant exécution le 2026-08-22) : 206 lignes
--   · juillet 2026, tous sports confondus ............ 194
--   · sports retirés encore présents en août 2026 ....  12
--     (esports, tabletennis, volleyball, handball —
--      RETIRED_SPORTS de core/constants.py)
--   Restent vivantes : 126 lignes d'août
--     (soccer 75, basketball 41, mma 9, baseball 1)
--
-- Les vues `monthly_performance`, `performance_summary` et `win_rate_monthly`
-- dérivent de `ai_learning_ledger` : elles se mettent à jour toutes seules,
-- il n'y a rien à y faire.
--
-- Idempotent : la table d'archive est créée si besoin, une ligne déjà
-- archivée n'est pas dupliquée (clé primaire conservée).
--
-- ⚠️ La borne d'affichage vit AUSSI dans le code : `PERF_START_MONTH`
-- (core/perf_view.py) empêche tout mois antérieur à août 2026 de réapparaître
-- sur /performance, y compris si des lignes de juillet étaient réinsérées.
-- Les deux se complètent : le SQL nettoie la table, le code tient la règle.

BEGIN;

CREATE TABLE IF NOT EXISTS ai_learning_ledger_archive
    (LIKE ai_learning_ledger INCLUDING ALL);
ALTER TABLE ai_learning_ledger_archive
    ADD COLUMN IF NOT EXISTS archived_at timestamptz NOT NULL DEFAULT now();

-- 1. Copie
INSERT INTO ai_learning_ledger_archive
SELECT l.*, now() AS archived_at
FROM ai_learning_ledger l
WHERE l.created_at < '2026-08-01T00:00:00Z'
   OR l.sport IN ('esports', 'tabletennis', 'volleyball', 'handball')
ON CONFLICT DO NOTHING;

-- 2. Retrait — UNIQUEMENT les lignes effectivement copiées
DELETE FROM ai_learning_ledger l
USING ai_learning_ledger_archive a
WHERE l.id = a.id;

COMMIT;

-- Vérification :
--   SELECT to_char(date_trunc('month',created_at),'YYYY-MM') AS mois,
--          sport, count(*) FROM ai_learning_ledger GROUP BY 1,2 ORDER BY 1,3 DESC;
--   -- attendu : uniquement 2026-08, et aucun sport de RETIRED_SPORTS
--   SELECT count(*) FROM ai_learning_ledger_archive;   -- attendu : 206
--
-- RESTAURATION (si l'on veut re-verser un sport ou un mois) :
--   BEGIN;
--   INSERT INTO ai_learning_ledger
--   SELECT <colonnes sauf archived_at> FROM ai_learning_ledger_archive
--    WHERE <critère> ON CONFLICT DO NOTHING;
--   DELETE FROM ai_learning_ledger_archive WHERE <critère>;
--   COMMIT;
--   -- puis abaisser PERF_START_MONTH, sinon le mois restera masqué.


-- ─────────────────────────────────────────────────────────────────────
-- COMPLÉMENT (même passe, 2026-08-22) : la table `signals`
--
-- 7 lignes de sports retirés y subsistaient (esports 4, tabletennis 3),
-- toutes en `settled`/`closed` — donc déjà invisibles sur le dashboard, qui
-- ne lit que `status='active'`. Elles sont archivées quand même : leurs
-- lignes de ledger viennent de partir, et laisser les signaux orphelins
-- derrière rendrait toute reconstitution ultérieure incohérente.
--
-- Aucune ligne ACTIVE n'est touchée : la clause l'interdit explicitement.

BEGIN;

CREATE TABLE IF NOT EXISTS signals_archive
    (LIKE signals INCLUDING ALL);
ALTER TABLE signals_archive
    ADD COLUMN IF NOT EXISTS archived_at timestamptz NOT NULL DEFAULT now();

INSERT INTO signals_archive
SELECT s.*, now() AS archived_at
FROM signals s
WHERE s.status <> 'active'
  AND (s.created_at < '2026-08-01T00:00:00Z'
       OR s.sport IN ('esports', 'tabletennis', 'volleyball', 'handball'))
ON CONFLICT DO NOTHING;

DELETE FROM signals s
USING signals_archive a
WHERE s.id = a.id
  AND s.status <> 'active';

COMMIT;

-- Vérification :
--   SELECT count(*) FROM signals
--    WHERE sport IN ('esports','tabletennis','volleyball','handball');  -- attendu : 0
