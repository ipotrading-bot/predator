-- migrate_v10_15_clv_archive.sql — 2026-09-10
--
-- POURQUOI
-- --------
-- L'audit du 2026-09-10 (règle 14 vérifiée ligne à ligne : 0 capture avant
-- émission, 0 après coup d'envoi, 0 tautologie) laisse deux familles de CLV
-- qui portent encore les moyennes affichées :
--
--   · closing_source = 'oracle' : la « clôture » était demandée à l'ancien
--     LLM (retiré le 2026-09-02), pas observée. 21 CLV du ledger. Hors de
--     l'esprit de la règle 14 : sans prix POSTÉRIEUR observé, la colonne
--     reste NULLE.
--   · |clv_pct_real| > 10 capturés AVANT le correctif du 2026-09-09 21:33
--     (b6e9937) : 13 lignes, dont Chindia–Voluntari à +55,4 % (clôture 1,158
--     contre un prix soft de 1,80 sur un h2h roumain de 2e rang) — presque
--     sûrement la mauvaise sélection côté exchange, pas un mouvement de
--     marché. Post-correctif (n=7) : étendue [−2,3 ; +6,0], aucun outlier.
--
-- ARCHIVER, JAMAIS SUPPRIMER (règle n°9)
-- --------------------------------------
-- Aucune ligne de résultat ne bouge. Les valeurs retirées des colonnes sont
-- d'abord copiées telles quelles dans `meta.clv_archive_v10_15` (JSON :
-- table, id, clv, prix de clôture, source, capture), puis seules les
-- colonnes de CLV repassent à NULL. `closing_pinnacle_price`,
-- `closing_line`, `closing_source` et `closing_captured_at` restent sur la
-- ligne : tout est recalculable. Idempotent : la seconde exécution ne
-- trouve plus rien à archiver (prédicat sur clv IS NOT NULL) et
-- `ON CONFLICT DO NOTHING` garde la première archive.

-- 1. Archive (une seule fois).
INSERT INTO meta (key, value, updated_at)
SELECT 'clv_archive_v10_15',
       coalesce(json_agg(row_to_json(t))::text, '[]'),
       now()
FROM (
    SELECT 'signals' AS tbl, id, clv_pct_real, clv_pct, closing_line,
           closing_pinnacle_price, closing_source, closing_captured_at
      FROM signals
     WHERE clv_pct_real IS NOT NULL
       AND (closing_source = 'oracle'
            OR (abs(clv_pct_real) > 10
                AND closing_captured_at < '2026-09-09T21:33:00+00:00'))
    UNION ALL
    SELECT 'ledger', signal_id, clv_pct_real, clv_final, NULL,
           closing_pinnacle_price, closing_source, closing_captured_at
      FROM ai_learning_ledger
     WHERE clv_pct_real IS NOT NULL
       AND (closing_source = 'oracle'
            OR (abs(clv_pct_real) > 10
                AND closing_captured_at < '2026-09-09T21:33:00+00:00'))
) t
ON CONFLICT (key) DO NOTHING;

-- 2. Les colonnes de CLV repassent à NULL sur ces mêmes lignes.
UPDATE signals
   SET clv_pct_real = NULL, clv_pct = NULL
 WHERE clv_pct_real IS NOT NULL
   AND (closing_source = 'oracle'
        OR (abs(clv_pct_real) > 10
            AND closing_captured_at < '2026-09-09T21:33:00+00:00'));

UPDATE ai_learning_ledger
   SET clv_pct_real = NULL, clv_final = NULL, was_clv_positive = NULL
 WHERE clv_pct_real IS NOT NULL
   AND (closing_source = 'oracle'
        OR (abs(clv_pct_real) > 10
            AND closing_captured_at < '2026-09-09T21:33:00+00:00'));

-- Vérification attendue : les deux SELECT rendent 0 ; l'archive existe.
--   SELECT count(*) FROM ai_learning_ledger WHERE clv_pct_real IS NOT NULL
--    AND (closing_source='oracle' OR (abs(clv_pct_real)>10
--         AND closing_captured_at < '2026-09-09T21:33:00+00:00'));
--   SELECT length(value) FROM meta WHERE key='clv_archive_v10_15';
