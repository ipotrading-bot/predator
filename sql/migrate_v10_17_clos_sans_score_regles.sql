-- ============================================================================
-- migrate_v10_17_clos_sans_score_regles.sql — QUATRE CLOS SANS SCORE RÉGLÉS
-- (2026-09-15, instruction opérateur « régler 1 »)
--
-- Quatre signaux sortis `closed` après EXPIRE_AFTER_H : aucune voie
-- automatique (ESPN, LiveScore, TheSportsDB) ne couvrait leur ligue. Scores
-- retrouvés à la main, DEUX sources indépendantes concordantes chacun, même
-- sémantique que core/settlement.determine_outcome :
--
--   10141  Moik Baku vs Simal (Azerbaïdjan 1. Liga, 11/09, RECOMMANDÉ)
--          1-1 — Moik Baku -0.5 @1.95 → LOSS
--          arena.az, sportfm.az, footlive.com
--   10120  CA Cerro vs CS Cerrito (Copa AUF Uruguay, 10/09, fantôme)
--          0-2, sans prolongation — CA Cerro -0.5 @2.15 → LOSS
--          ladiaria.com.uy, eltelegrafo.com
--   10150  Breidablik vs Afturelding (finale coupe d'Islande, 11/09, fantôme)
--          2-1 en 90 min — Over 3.5 @1.95 → LOSS
--          fotbolti.net, visir.is (mbl.is par recherche)
--   10203  Young Lions FC vs FC Jurong (Singapore PL, 13/09, fantôme)
--          0-4 (FC Jurong = ex-Albirex Niigata Singapore) — Young Lions +2.0 → LOSS
--          fotmob.com, littlebigreddot.com
--
-- Précédent : contre-audit du 2026-09-08 (12 issues passées à leur issue
-- réelle sur pièces). Les gardes `outcome IN ('closed','expired')` et
-- `status='closed'` rendent le script sans effet s'il est rejoué ou si une
-- voie automatique a réglé entre-temps.
--
-- AVANT (lecture seule) — 4 lignes attendues, outcome 'closed' :
--   SELECT signal_id, outcome FROM ai_learning_ledger
--    WHERE signal_id IN (10141, 10120, 10150, 10203);
-- ============================================================================

BEGIN;

UPDATE ai_learning_ledger
   SET outcome = 'LOSS'
 WHERE signal_id IN (10141, 10120, 10150, 10203)
   AND outcome IN ('closed', 'expired');

UPDATE signals
   SET status = 'settled', outcome = 'LOSS'
 WHERE id IN (10141, 10120, 10150, 10203)
   AND status = 'closed' AND outcome IS NULL;

COMMIT;

-- TÉMOINS : 4 × LOSS dans les deux tables.
--   SELECT signal_id, outcome FROM ai_learning_ledger WHERE signal_id IN (10141,10120,10150,10203);
--   SELECT id, status, outcome FROM signals WHERE id IN (10141,10120,10150,10203);
-- RETOUR ARRIÈRE : mêmes UPDATE vers outcome='closed' (ledger) et
-- status='closed', outcome=NULL (signals).
