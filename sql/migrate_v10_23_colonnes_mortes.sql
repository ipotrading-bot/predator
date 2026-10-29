-- ============================================================================
-- migrate_v10_23_colonnes_mortes.sql — QUATRE COLONNES JAMAIS ÉCRITES SONT
-- RETIRÉES DU LEDGER (2026-10-29)
--
-- OBJECTIF
-- --------
-- Mesuré le 2026-10-29 : sur `ai_learning_ledger` (1 357 lignes) ET
-- `ai_learning_ledger_archive` (283 lignes), ces colonnes ne portent AUCUNE
-- valeur :
--   · sharp_divergence_std               — remplacée par sharp_divergence_pts (v10_22)
--   · ai_confidence_score                — couche IA de règlement retirée (2026-09-02)
--   · news_sentiment_score               — idem
--   · bookmaker_adjustment_speed_seconds — jamais câblée
-- Une colonne toujours NULLE fait croire à une donnée qui n'existe pas : toute
-- analyse du ledger la lit, la trouve vide, et s'en étonne.
--
-- Aucune ligne n'est supprimée (règle 9) : seules des colonnes VIDES partent,
-- et le bloc de garde ci-dessous REFUSE la migration si l'une d'elles a reçu
-- une valeur entre-temps.
--
-- ⚠️ ORDRE : APRÈS le déploiement du moteur qui n'écrit plus
-- `sharp_divergence_std` (core/db.log_to_ledger, backfill_ledger.py). Appliquée
-- avant, chaque règlement échouerait sur la colonne disparue.
--
-- ⚠️ RESTAURATION des archives v10_16 et v10_19 : leurs blocs de restauration
-- nomment ces quatre colonnes ; les retirer de la liste avant de les rejouer.
-- ============================================================================

DO $$
DECLARE
  t text;
  n bigint;
BEGIN
  FOREACH t IN ARRAY ARRAY['ai_learning_ledger', 'ai_learning_ledger_archive'] LOOP
    EXECUTE format(
      'SELECT count(*) FROM %I WHERE sharp_divergence_std IS NOT NULL
          OR ai_confidence_score IS NOT NULL OR news_sentiment_score IS NOT NULL
          OR bookmaker_adjustment_speed_seconds IS NOT NULL', t) INTO n;
    IF n > 0 THEN
      RAISE EXCEPTION '% : % ligne(s) portent une valeur — rien n''est retiré', t, n;
    END IF;
  END LOOP;
END $$;

ALTER TABLE ai_learning_ledger
  DROP COLUMN IF EXISTS sharp_divergence_std,
  DROP COLUMN IF EXISTS ai_confidence_score,
  DROP COLUMN IF EXISTS news_sentiment_score,
  DROP COLUMN IF EXISTS bookmaker_adjustment_speed_seconds;

ALTER TABLE ai_learning_ledger_archive
  DROP COLUMN IF EXISTS sharp_divergence_std,
  DROP COLUMN IF EXISTS ai_confidence_score,
  DROP COLUMN IF EXISTS news_sentiment_score,
  DROP COLUMN IF EXISTS bookmaker_adjustment_speed_seconds;

-- Vérification :
-- SELECT table_name, column_name FROM information_schema.columns
--  WHERE column_name IN ('sharp_divergence_std', 'ai_confidence_score',
--                        'news_sentiment_score', 'bookmaker_adjustment_speed_seconds');
-- → 0 ligne.
