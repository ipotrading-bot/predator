-- ============================================================================
-- migrate_v10_22_ecart_sharp.sql — L'ACCORD DES DEUX AVIS SHARP EST GARDÉ
-- (2026-10-29)
--
-- OBJECTIF
-- --------
-- run_engine._enrich_from_exchange compare Pinnacle à l'exchange (Matchbook,
-- Smarkets) sur chaque match où les deux cotent : au-delà de
-- EXCHANGE_DIVERGENCE_PTS le match est refusé, en deçà l'écart était loggé
-- puis JETÉ. C'est pourtant la seule mesure du ledger qui dise à quel point le
-- prix sharp d'entrée était sûr. Mesuré le 2026-10-29 : `sharp_divergence_std`
-- (prévue pour ce rôle) n'a jamais été écrite — 0 valeur sur 1 357 lignes —,
-- et `consensus_score` vaut 100 sur toutes, l'exchange étant exclu du juge.
--
-- CE QUE FAIT CETTE MIGRATION
-- ---------------------------
--   1. `signals.sharp_divergence_pts numeric` — écart moneyline du MATCH entre
--      Pinnacle et l'exchange, en points de probabilité ; NULL quand une seule
--      source sharp cotait le match. Rafraîchi avec le prix.
--   2. Même colonne sur `ai_learning_ledger`, recopiée au règlement
--      (core/db.log_to_ledger).
--   3. Même colonne sur les deux archives (`LIKE` figé à v10_5).
--   Aucun backfill : l'écart des lignes anciennes n'a été stocké nulle part.
--
-- Idempotent : ADD COLUMN IF NOT EXISTS. Aucune suppression (règle 9).
--
-- ⚠️ À APPLIQUER AVANT LE DÉPLOIEMENT DU MOTEUR : `python scripts/ops.py
-- supabase migrate sql/migrate_v10_22_ecart_sharp.sql`. Sans elle, le repli de
-- schéma de run_engine._save retire TOUTES les colonnes optionnelles,
-- `is_shadow` compris, et le rafraîchissement d'un signal échoue.
-- ============================================================================

ALTER TABLE signals
  ADD COLUMN IF NOT EXISTS sharp_divergence_pts numeric;

ALTER TABLE ai_learning_ledger
  ADD COLUMN IF NOT EXISTS sharp_divergence_pts numeric;

ALTER TABLE signals_archive
  ADD COLUMN IF NOT EXISTS sharp_divergence_pts numeric;

ALTER TABLE ai_learning_ledger_archive
  ADD COLUMN IF NOT EXISTS sharp_divergence_pts numeric;

-- Vérification :
-- SELECT table_name FROM information_schema.columns
--  WHERE column_name = 'sharp_divergence_pts' ORDER BY 1;
-- → 4 lignes.
