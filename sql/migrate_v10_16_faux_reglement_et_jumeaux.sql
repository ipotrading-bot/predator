-- ============================================================================
-- migrate_v10_16_faux_reglement_et_jumeaux.sql — AUDIT 72 H (2026-09-15)
--
-- QUATRE LIGNES DE LEDGER ARCHIVÉES, DEUX SIGNAUX CLOS, UN SEUIL RETIRÉ.
-- Règle n°9 : on ARCHIVE, on ne supprime pas sèchement (bloc RESTAURATION).
--
-- A. FAUX RÈGLEMENT — signaux 10198 et 10199 (fantômes)
--    « Real Valladolid CF vs Real Sociedad », ligue « Spain - Division de
--    Honor Juvenil - Grupo II » (U19), coup d'envoi 2026-09-13 10:00 UTC.
--    Réglés par ESPN à 16:37 sur « Real Oviedo at Real Valladolid 0-3 »
--    (esp.2, 14:15Z, SENIORS, autre adversaire). Deux causes corrigées dans
--    le code le même jour : « juvenil » absent de `_LIGUE_JEUNES`, et
--    `strict_team_match("Real Sociedad", "Real Oviedo")` vrai (ratio 0,75).
--    L'issue réelle du match U19 n'est pas connue : les lignes sortent du
--    ledger, les signaux passent `closed` sans issue (même état qu'un signal
--    sans score, cf. 10141) — une issue fausse vaut moins qu'une issue absente.
--
-- B. JUMEAUX INTER-SOURCES À LIBELLÉS DIFFÉRENTS — liste d'ids MORTE
--    Vérifiés un à un (même book, même marché, même cote, même coup d'envoi,
--    créés à < 2 min d'écart) ; on garde la ligne la plus ancienne :
--      · 10219 « Como vs Parma » (OddsAPI)         ← jumeau de 10216
--        « Como 1907 vs Parma Calcio » (odds-api.io), Over 2.5 @1.71, WIN/WIN
--      · 10182 « Le Havre AC vs Angers SCO »       ← jumeau de 10181
--        « Le Havre vs Angers », Over 2.5 @1.97, LOSS/LOSS
--    Aucune règle floue en SQL (INCIDENTS, « Le même match réel pesait
--    DOUBLE ») : deux ids, rien d'autre.
--
-- C. SEUIL GONFLÉ PAR LE CLIQUET — threshold_seg_baseball_totals = 4.6
--    +0,4 à chaque audit sur le MÊME échantillon (n=20), sans une ligne
--    nouvelle : la valeur ne résulte d'aucune mesure (règle 10). Retirée ; le
--    premier audit du code corrigé décide UNE fois sur cet échantillon et
--    mémorise son empreinte (`meta.learning_bases`).
--    ⚠️ À APPLIQUER APRÈS LE DÉPLOIEMENT du correctif : l'ancien code
--    relancerait le cliquet depuis le seuil du sport.
--    `edge_ceiling_soccer` (6.0, posé sur n=5) n'est PAS touché ici : le code
--    corrigé le retire de lui-même au prochain audit (_drop_stale_ceiling).
--
-- AVANT APPLICATION (lecture seule) — doit rendre 4 lignes, issues ci-dessus :
--   SELECT signal_id, outcome, match FROM ai_learning_ledger
--    WHERE signal_id IN (10198, 10199, 10219, 10182);
-- ============================================================================

BEGIN;

INSERT INTO ai_learning_ledger_archive
      (id, signal_id, sport, league, market_type, time_to_match_minutes,
       initial_edge, sharp_divergence_std, ai_confidence_score,
       news_sentiment_score, clv_final, was_clv_positive,
       bookmaker_adjustment_speed_seconds, created_at, match, outcome, odds,
       selection, market, kelly_pct, closing_pinnacle_price, clv_pct_real,
       sharp_prob, sharp_sources, consensus_score, closing_captured_at,
       closing_source, is_shadow, soft_book, stake_xof, archived_at)
SELECT id, signal_id, sport, league, market_type, time_to_match_minutes,
       initial_edge, sharp_divergence_std, ai_confidence_score,
       news_sentiment_score, clv_final, was_clv_positive,
       bookmaker_adjustment_speed_seconds, created_at, match, outcome, odds,
       selection, market, kelly_pct, closing_pinnacle_price, clv_pct_real,
       sharp_prob, sharp_sources, consensus_score, closing_captured_at,
       closing_source, is_shadow, soft_book, stake_xof, now()
  FROM ai_learning_ledger
 WHERE signal_id IN (10198, 10199, 10219, 10182);

DELETE FROM ai_learning_ledger
 WHERE signal_id IN (10198, 10199, 10219, 10182);

UPDATE signals
   SET status = 'closed', outcome = NULL
 WHERE id IN (10198, 10199) AND status = 'settled';

DELETE FROM meta WHERE key = 'threshold_seg_baseball_totals';

COMMIT;

-- TÉMOINS après application :
--   SELECT count(*) FROM ai_learning_ledger WHERE signal_id IN (10198,10199,10219,10182);  -- 0
--   SELECT count(*) FROM ai_learning_ledger_archive
--    WHERE signal_id IN (10198,10199,10219,10182) AND archived_at::date = current_date;   -- 4
--   SELECT id, status, outcome FROM signals WHERE id IN (10198, 10199);                   -- closed, NULL
--
-- RESTAURATION (si une ligne s'avérait légitime) :
--   BEGIN;
--   INSERT INTO ai_learning_ledger (<colonnes ci-dessus sauf archived_at>)
--   SELECT <mêmes colonnes> FROM ai_learning_ledger_archive
--    WHERE signal_id = <id> AND archived_at::date = '2026-09-15';
--   DELETE FROM ai_learning_ledger_archive
--    WHERE signal_id = <id> AND archived_at::date = '2026-09-15';
--   COMMIT;
