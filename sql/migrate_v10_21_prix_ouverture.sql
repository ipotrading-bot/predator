-- ============================================================================
-- migrate_v10_21_prix_ouverture.sql — LE PRIX DE LA PREMIÈRE VUE EST GARDÉ
-- (2026-10-23)
--
-- OBJECTIF
-- --------
-- Le basket repéré à plus de T-6h devient fantôme `basket_avant_effectifs`
-- (run_engine.py, BASKET_AVANT_EFFECTIFS_MIN) et passe RECOMMANDÉ si un scan
-- à T-2..6h le revoit avec un edge. Ce rafraîchissement RÉÉCRIT sharp_prob et
-- xbet_odd sur la ligne : sans copie, le prix de T-10h disparaît, et les seuls
-- fantômes qui restent sont ceux dont l'edge s'est évanoui — l'échantillon de
-- la règle de réouverture serait biaisé contre la réouverture par
-- construction. Ces deux colonnes gardent le prix de la PREMIÈRE insertion,
-- pour tous les sports : la calibration se mesure aussi depuis l'ouverture.
--
-- CE QUE FAIT CETTE MIGRATION
-- ---------------------------
--   1. `signals.ouverture_sharp_prob numeric`, `signals.ouverture_cote numeric`
--      — posées par run_engine._save à l'INSERT, FIGÉES au rafraîchissement
--      (_FIGES_AU_RAFRAICHISSEMENT).
--   2. Mêmes colonnes sur `ai_learning_ledger`, recopiées du signal au
--      règlement (core/db.log_to_ledger).
--   3. Mêmes colonnes sur les deux archives (`LIKE` figé à v10_5).
--   Aucun backfill : le prix de première vue des lignes anciennes n'a été
--   stocké nulle part, on ne le devine pas (NULL = inconnu).
--
-- Idempotent : ADD COLUMN IF NOT EXISTS. Aucune suppression (règle 9).
--
-- ⚠️ À APPLIQUER AVANT LE DÉPLOIEMENT DU MOTEUR : `python scripts/ops.py
-- supabase migrate sql/migrate_v10_21_prix_ouverture.sql`. Sans elle, le repli
-- de schéma de run_engine._save retire TOUTES les colonnes optionnelles,
-- `is_shadow` compris — un fantôme basket partirait recommandé en base.
-- ============================================================================

ALTER TABLE signals
  ADD COLUMN IF NOT EXISTS ouverture_sharp_prob numeric,
  ADD COLUMN IF NOT EXISTS ouverture_cote numeric;

ALTER TABLE ai_learning_ledger
  ADD COLUMN IF NOT EXISTS ouverture_sharp_prob numeric,
  ADD COLUMN IF NOT EXISTS ouverture_cote numeric;

ALTER TABLE signals_archive
  ADD COLUMN IF NOT EXISTS ouverture_sharp_prob numeric,
  ADD COLUMN IF NOT EXISTS ouverture_cote numeric;

ALTER TABLE ai_learning_ledger_archive
  ADD COLUMN IF NOT EXISTS ouverture_sharp_prob numeric,
  ADD COLUMN IF NOT EXISTS ouverture_cote numeric;

-- Vérification :
-- SELECT table_name, column_name FROM information_schema.columns
--  WHERE column_name IN ('ouverture_sharp_prob', 'ouverture_cote') ORDER BY 1, 2;
-- → 8 lignes.
