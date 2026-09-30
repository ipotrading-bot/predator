-- ============================================================================
-- migrate_v10_19_jumeaux_inter_sources.sql — LE MÊME PARI RÉGLÉ DEUX FOIS
-- (2026-09-30)
--
-- OBJECTIF
-- --------
-- Archiver 7 lignes de ledger : le même pari, sur le même match réel, arrivé
-- par deux sources sous deux match_id et deux libellés différents
-- (« Montréal » / « Montreal », « Como » / « Como 1907 »…). La garde exacte
-- `core/db.py::_ledger_jumeau_reel` ne les voyait pas ; v10_10 n'avait
-- épinglé que les jumeaux flous connus au 2026-09-02.
-- Toronto–Montréal Over 6.5 figurait DEUX FOIS dans l'historique
-- /performance, deux pertes pour un seul pari (plainte opérateur du
-- 2026-09-30, capture de la page).
--
-- COMMENT LA LISTE A ÉTÉ FAITE
-- ----------------------------
-- `run_engine._meme_match_reel` (même sport, deux camps appariés, coup
-- d'envoi à 30 min) rejoué sur les 677 signaux en base : 18 paires de
-- jumeaux, dont 10 portent le MÊME pari (marché et sélection identiques).
-- 7 de ces 10 ont encore leurs deux lignes vivantes au ledger ; les autres
-- ont été archivées par v10_10 ou n'ont écrit qu'une ligne. Chaque paire
-- a été vérifiée : même heure, même issue.
--
-- QUELLE LIGNE RESTE : le signal ANNONCÉ en premier, ou le recommandé
-- quand l'autre était un fantôme (Cagliari, Elche). Toutes les paires ont
-- la même issue : le bilan ne change que par le poids double retiré.
--
-- LISTE MORTE, aucune règle floue en SQL (même parti pris que v10_10).
-- Ce script ne DÉTRUIT rien (règle dure n°9) : il DÉPLACE vers
-- `ai_learning_ledger_archive` puis retire les seules lignes copiées, dans
-- la même transaction. Idempotent (ON CONFLICT DO NOTHING).
-- ============================================================================

BEGIN;

-- Colonnes NOMMÉES : l'archive porte deux colonnes héritées
-- (actual_result, profit_units) et `archived_at` au milieu — une copie
-- positionnelle `l.*` se décalerait (motif v10_16).
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
FROM ai_learning_ledger l
WHERE l.id IN (
    'cb1d6238-9219-4ad1-a852-f7b3aa433869',  -- Toronto Maple Leafs vs Montreal Canadiens, Over 6.5 LOSS (sid 10571) : jumeau de « … Montréal Canadiens » (sid 10565, annoncé en premier, gardé)
    'c0250e95-31ac-45b7-82be-9c24913d65fb',  -- Newcastle United vs Bournemouth, h2h PUSH (sid 9945) : jumeau de « Newcastle United vs AFC Bournemouth » (sid 9944, gardé)
    'd6636d16-297d-4105-8292-37f7cd7158ef',  -- Cagliari Calcio vs US Lecce, Under 2.5 WIN, fantôme (sid 10049) : jumeau de « Cagliari vs Lecce » (sid 10037, recommandé, gardé)
    'dd506812-e290-4600-9a52-2821aba9c3ed',  -- Elche CF vs Real Sociedad San Sebastian, Over 2.5 WIN, fantôme (sid 10051) : jumeau de « Elche CF vs Real Sociedad » (sid 10048, recommandé, gardé)
    '59b0650c-d1e2-47c1-834b-9eb9c406fe3b',  -- Como 1907 vs RB Leipzig, Under 3.5 LOSS (sid 10126) : jumeau de « Como vs RB Leipzig » (sid 10124, gardé)
    '9bd8c510-bedd-442a-8740-e73c274c959e',  -- Marseille vs Paris Saint Germain, Under 3.5 WIN (sid 10378) : jumeau de « Olympique Marseille vs Paris Saint-Germain » (sid 10377, gardé)
    'f06c5ba8-bd12-495d-8a7a-2838932771ba'   -- Tigres vs Puebla, Under 2.5 WIN (sid 10499) : jumeau de « Tigres UANL vs Club Puebla » (sid 10497, gardé)
)
ON CONFLICT DO NOTHING;

DELETE FROM ai_learning_ledger l
USING ai_learning_ledger_archive a
WHERE l.id = a.id;

COMMIT;

-- ── RESTAURATION (seulement après vérification sur pièces) ────────────────
--   BEGIN;
--   INSERT INTO ai_learning_ledger
--   SELECT <colonnes sauf archived_at> FROM ai_learning_ledger_archive
--    WHERE id = '<uuid>'
--   ON CONFLICT DO NOTHING;
--   DELETE FROM ai_learning_ledger_archive WHERE id = '<uuid>';
--   COMMIT;

-- ── SELECT TÉMOINS après application ───────────────────────────────────────
-- (1) Les 7 lignes sont en archive :
--   SELECT count(*) FROM ai_learning_ledger_archive WHERE id IN (<les 7 uuid>);
--   -- attendu : 7
-- (2) Plus aucune au ledger vivant :
--   SELECT count(*) FROM ai_learning_ledger WHERE id IN (<les 7 uuid>);
--   -- attendu : 0
-- (3) Toronto n'y figure plus qu'une fois :
--   SELECT count(*) FROM ai_learning_ledger WHERE match ILIKE 'Toronto Maple Leafs vs Montr%';
--   -- attendu : 1
