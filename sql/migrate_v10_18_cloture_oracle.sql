-- migrate_v10_18_cloture_oracle.sql — 2026-09-29
--
-- POURQUOI
-- --------
-- La migration v10_15 (2026-09-10) a retiré le CLV des lignes dont la
-- « clôture » avait été DEMANDÉE à l'ancien LLM (closing_source = 'oracle',
-- core/oracle.py, supprimé le 2026-09-02), mais a laissé le PRIX de clôture
-- sur la ligne (« tout est recalculable »). Or depuis le 2026-09-27 la couche
-- d'apprentissage ne lit plus le CLV : elle lit la DÉRIVE sharp
-- (core/learning_layer._derive_stats), calculée sur `closing_pinnacle_price`
-- — sans regarder la source. Mesuré le 2026-09-29 : 49 lignes du ledger
-- (02→30/08) et 20 signaux (01→25/08) portent un prix inventé que tout
-- lecteur du prix prend pour une observation. Aucune n'entrait dans les
-- mesures appliquées ce jour-là (signaux antérieurs à CALIBRATION_EPOCH) :
-- défaut latent, pas actif. Règle 14 : sans prix POSTÉRIEUR observé, la
-- colonne reste NULLE — le prix, pas seulement le CLV.
--
-- `_derive_stats` filtre désormais `closing_source = 'oracle'` (même commit) ;
-- cette migration retire la donnée elle-même, pour tous les lecteurs à venir.
--
-- ARCHIVER, JAMAIS SUPPRIMER (règle n°9)
-- --------------------------------------
-- Aucune ligne ne bouge et aucune issue ne change. Les valeurs retirées sont
-- d'abord copiées telles quelles dans `meta.closing_oracle_archive_v10_18`
-- (JSON : table, id, prix, capture, source), puis seuls
-- `closing_pinnacle_price` et `closing_captured_at` repassent à NULL.
-- `closing_source = 'oracle'` RESTE sur la ligne : la provenance est gardée,
-- « l'oracle a été interrogé, sa réponse est retirée ».
-- `ai_learning_ledger_archive` n'est pas touchée : c'est déjà une archive,
-- aucun lecteur de production ne la lit.
-- Idempotent : la seconde exécution ne trouve plus rien (prédicat sur
-- closing_pinnacle_price IS NOT NULL) et `ON CONFLICT DO NOTHING` garde la
-- première archive.
--
-- AVANT (lecture seule) — 49 et 20 attendus :
--   SELECT count(*) FROM ai_learning_ledger
--    WHERE closing_source = 'oracle' AND closing_pinnacle_price IS NOT NULL;
--   SELECT count(*) FROM signals
--    WHERE closing_source = 'oracle' AND closing_pinnacle_price IS NOT NULL;

BEGIN;

-- 1. Archive (une seule fois).
INSERT INTO meta (key, value, updated_at)
SELECT 'closing_oracle_archive_v10_18',
       coalesce(json_agg(row_to_json(t))::text, '[]'),
       now()
FROM (
    SELECT 'signals' AS tbl, id, closing_pinnacle_price, closing_captured_at,
           closing_source
      FROM signals
     WHERE closing_source = 'oracle' AND closing_pinnacle_price IS NOT NULL
    UNION ALL
    SELECT 'ledger', signal_id, closing_pinnacle_price, closing_captured_at,
           closing_source
      FROM ai_learning_ledger
     WHERE closing_source = 'oracle' AND closing_pinnacle_price IS NOT NULL
) t
ON CONFLICT (key) DO NOTHING;

-- 2. Le prix inventé et sa date repassent à NULL sur ces mêmes lignes.
UPDATE signals
   SET closing_pinnacle_price = NULL, closing_captured_at = NULL
 WHERE closing_source = 'oracle' AND closing_pinnacle_price IS NOT NULL;

UPDATE ai_learning_ledger
   SET closing_pinnacle_price = NULL, closing_captured_at = NULL
 WHERE closing_source = 'oracle' AND closing_pinnacle_price IS NOT NULL;

COMMIT;

-- TÉMOINS : les deux premiers SELECT rendent 0 ; l'archive compte 69 entrées.
--   SELECT count(*) FROM ai_learning_ledger
--    WHERE closing_source = 'oracle' AND closing_pinnacle_price IS NOT NULL;
--   SELECT count(*) FROM signals
--    WHERE closing_source = 'oracle' AND closing_pinnacle_price IS NOT NULL;
--   SELECT json_array_length(value::json) FROM meta
--    WHERE key = 'closing_oracle_archive_v10_18';
-- RETOUR ARRIÈRE : rejouer les prix depuis l'archive JSON (table, id).
