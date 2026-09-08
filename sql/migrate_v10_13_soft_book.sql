-- ============================================================================
-- migrate_v10_13_soft_book.sql — LE BOOK D'ORIGINE DEVIENT UNE COLONNE (2026-09-08)
--
-- OBJECTIF
-- --------
-- Le 2026-09-07, « Al-Adalah +0.5 @ 1.85 » sortait d'un line shopping
-- Bet365 + 1xbet sous une fiche « 1XBET » : 1xbet ne cotait pas cette ligne,
-- et le book d'origine n'était stocké nulle part. L'incident a posé la
-- condition du retour d'un second book d'exécution : le book d'origine
-- STOCKÉ par ligne et affiché sur la fiche. Le 2026-09-08 l'opérateur a
-- rouvert Bet365 (core.constants.EXECUTION_BOOKS) ; cette colonne est cette
-- condition.
--
-- CE QUE FAIT CETTE MIGRATION
-- ---------------------------
--   1. `signals.soft_book text` — posé par run_engine._emit/_save à chaque
--      émission ET rafraîchi avec le prix (jamais figé : un book et un prix
--      de deux ticks différents seraient la fiche mensongère du 2026-09-07).
--      NULL = prix sans attribution (repli sharp d'une source sans book
--      d'exécution, ou ligne d'avant cette migration).
--   2. `ai_learning_ledger.soft_book` — recopié du signal au règlement
--      (core/db.log_to_ledger) : la performance se mesure PAR book
--      (règle 7), pas seulement « le soft ».
--   3. Mêmes colonnes sur les deux archives (`LIKE` figé à v10_5).
--   4. BACKFILL honnête : entre le 2026-09-07 14:37 UTC (commit 8b6ea55,
--      1xbet seul book d'exécution) et cette migration, tout prix soft est
--      1xbet par construction → '1xbet'. AVANT cette date le prix pouvait
--      venir de Bet365 sans trace : ces lignes restent NULL, on ne devine
--      pas. Le ledger hérite par signal_id. Aucune suppression (règle 9).
--
-- Idempotent : ADD COLUMN IF NOT EXISTS ; les UPDATE ne touchent que les
-- lignes encore NULL dans la plage.
--
-- À APPLIQUER : `python scripts/ops.py supabase migrate
-- sql/migrate_v10_13_soft_book.sql`. Sans elle, run_engine._save retire la
-- colonne (_OPTIONAL_COLS) et l'INSERT passe — le book est alors perdu en
-- silence, pas le signal.
-- ============================================================================

ALTER TABLE signals
  ADD COLUMN IF NOT EXISTS soft_book text;

ALTER TABLE ai_learning_ledger
  ADD COLUMN IF NOT EXISTS soft_book text;

ALTER TABLE signals_archive
  ADD COLUMN IF NOT EXISTS soft_book text;

ALTER TABLE ai_learning_ledger_archive
  ADD COLUMN IF NOT EXISTS soft_book text;

-- Backfill : régime « 1xbet seul » (2026-09-07 14:37 UTC → maintenant).
UPDATE signals
   SET soft_book = '1xbet'
 WHERE soft_book IS NULL
   AND scanned_at >= '2026-09-07T14:37:00Z';

UPDATE ai_learning_ledger l
   SET soft_book = s.soft_book
  FROM signals s
 WHERE l.soft_book IS NULL
   AND l.signal_id = s.id
   AND s.soft_book IS NOT NULL;
