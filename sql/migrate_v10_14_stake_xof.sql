-- ============================================================================
-- migrate_v10_14_stake_xof.sql — LA MISE EN FRANCS DEVIENT UNE COLONNE (2026-09-09)
--
-- OBJECTIF
-- --------
-- Décision opérateur du 2026-09-09 : bankroll MENSUELLE de 100 000 F, posée
-- le 1er, à dépenser en entier sur le mois ; budget du jour réparti entre les
-- signaux au prorata de leur Kelly (core/bankroll.py). La mise d'un signal
-- est décidée à l'ÉMISSION et doit être retrouvée telle quelle au règlement
-- et sur la page /bank — `kelly_pct` (un pourcentage) ne suffit plus : la
-- même fraction ne donne pas la même mise selon le jour du mois.
--
-- CE QUE FAIT CETTE MIGRATION
-- ---------------------------
--   1. `signals.stake_xof integer` — posé par run_engine (après
--      _shadow_partition, recommandés seulement) et FIGÉ au premier
--      enregistrement (_FIGES_AU_RAFRAICHISSEMENT) : un rafraîchissement de
--      prix ne change pas une mise peut-être déjà posée. NULL = fantôme, ou
--      ligne d'avant cette migration.
--   2. `ai_learning_ledger.stake_xof` — recopié du signal au règlement
--      (core/db.log_to_ledger) : c'est là que la mise est ACTÉE.
--   3. Mêmes colonnes sur les deux archives.
--   4. PAS de backfill : une mise qu'on n'a pas décidée à l'époque ne se
--      devine pas en base. La page /bank RECONSTITUE les lignes NULL à
--      l'affichage (core/bankroll.reconstitute) et les marque comme telles.
--
-- Idempotent : ADD COLUMN IF NOT EXISTS.
--
-- À APPLIQUER : `python scripts/ops.py supabase migrate
-- sql/migrate_v10_14_stake_xof.sql`. Sans elle, run_engine._save retire la
-- colonne (_OPTIONAL_COLS) et l'INSERT passe — la mise est alors perdue en
-- silence (reconstituée à l'affichage), pas le signal.
-- ============================================================================

ALTER TABLE signals
  ADD COLUMN IF NOT EXISTS stake_xof integer;

ALTER TABLE ai_learning_ledger
  ADD COLUMN IF NOT EXISTS stake_xof integer;

ALTER TABLE signals_archive
  ADD COLUMN IF NOT EXISTS stake_xof integer;

ALTER TABLE ai_learning_ledger_archive
  ADD COLUMN IF NOT EXISTS stake_xof integer;
