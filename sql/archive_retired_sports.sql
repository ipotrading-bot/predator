-- sql/archive_retired_sports.sql — ARCHIVAGE des sports retirés (Mission 2, Phase 1)
--
-- À EXÉCUTER À LA MAIN, par l'opérateur, dans le SQL Editor Supabase — JAMAIS
-- par un workflow. Ce script ne SUPPRIME rien : il DÉPLACE les lignes des
-- sports retirés (eSports, tennis de table, volleyball, handball) vers
-- `ai_learning_ledger_archive` (même schéma + `archived_at`), puis les retire
-- de la table vivante dans la MÊME transaction.
--
-- POURQUOI ARCHIVER ET NON EFFACER : ces lignes sont la seule trace
-- empirique (cotes, edge d'entrée, CLV, issue réelle) qui permettrait de
-- rouvrir un de ces sports plus tard sur des chiffres, et tout backtest futur
-- qui ignorerait ces paris réglés serait faussé (biais de survie). Le
-- dashboard les cache déjà (core/perf_view.py) : l'archivage est une
-- question d'hygiène de la table vivante, pas d'affichage — il est donc
-- OPTIONNEL et réversible (voir le bloc RESTAURATION en bas).
--
-- Idempotent : la table d'archive est créée si besoin, les lignes déjà
-- archivées ne sont pas dupliquées (clé primaire conservée).

BEGIN;

CREATE TABLE IF NOT EXISTS ai_learning_ledger_archive
    (LIKE ai_learning_ledger INCLUDING ALL);
ALTER TABLE ai_learning_ledger_archive
    ADD COLUMN IF NOT EXISTS archived_at timestamptz NOT NULL DEFAULT now();

-- 1. Copie (ON CONFLICT sur la clé primaire : déjà archivé → ignoré)
INSERT INTO ai_learning_ledger_archive
SELECT l.*, now() AS archived_at
FROM ai_learning_ledger l
WHERE l.sport IN ('esports', 'tabletennis', 'volleyball', 'handball')
ON CONFLICT DO NOTHING;

-- 2. Retrait de la table vivante — UNIQUEMENT les lignes effectivement copiées
DELETE FROM ai_learning_ledger l
USING ai_learning_ledger_archive a
WHERE l.id = a.id
  AND l.sport IN ('esports', 'tabletennis', 'volleyball', 'handball');

COMMIT;

-- Vérification suggérée :
--   SELECT sport, count(*) FROM ai_learning_ledger_archive GROUP BY sport;
--   SELECT count(*) FROM ai_learning_ledger
--    WHERE sport IN ('esports','tabletennis','volleyball','handball');  -- attendu : 0

-- RESTAURATION (si un sport est rouvert) :
--   BEGIN;
--   INSERT INTO ai_learning_ledger
--   SELECT <toutes les colonnes sauf archived_at> FROM ai_learning_ledger_archive
--    WHERE sport = '<sport>' ON CONFLICT DO NOTHING;
--   DELETE FROM ai_learning_ledger_archive WHERE sport = '<sport>';
--   COMMIT;
