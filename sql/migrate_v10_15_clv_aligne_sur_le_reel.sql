-- migrate_v10_15_clv_aligne_sur_le_reel.sql — 2026-09-09 (soir)
--
-- POURQUOI CETTE SECONDE PASSE
-- ----------------------------
-- `migrate_v10_14` n'effaçait le faux CLV que sur les lignes SANS aucune
-- trace de clôture. Filtre trop indulgent : une ligne peut porter un vrai
-- `clv_pct_real` (capturé par core/closing_line.py) et garder À CÔTÉ un
-- `clv_pct` / `clv_final` écrit par le règlement, qui vaut l'edge d'ENTRÉE.
-- La capture réelle la sauvait du filtre, la tautologie restait.
--
-- La preuve, trouvée en vérifiant l'effet de v10_14 — une ligne du ledger :
--
--     clv_final = +1.84   ·   clv_pct_real = -1.52   ·   outcome = LOSS
--
-- Le pari a PERDU la clôture de 1,52 point et la colonne annonçait +1,84.
-- Après v10_14 il restait 132 lignes de `signals` et 140 du ledger dans ce
-- cas — les deux tiers de ce qui subsistait.
--
-- LA RÈGLE APPLIQUÉE ICI
-- ----------------------
-- Une colonne de CLV vaut la mesure réelle, ou rien (règle dure n°14) :
--   1. `clv_pct_real` renseigné  → la colonne recopie CETTE valeur ;
--   2. sinon, prix de clôture observé → on RECALCULE depuis lui
--      (cote prise / clôture - 1), ce qui est une vraie observation ;
--   3. sinon → NULL, déjà fait par v10_14.
--
-- Aucune ligne de résultat n'est supprimée (règle n°9), et l'edge d'entrée
-- reste lisible : `edge_pct` / `initial_edge` sur la même ligne, et l'écart
-- de prix se recalcule depuis `xbet_odd` et `pinnacle_price`.
--
-- Idempotente : rejouée, elle réécrit les mêmes valeurs.

-- ── signals ──────────────────────────────────────────────────────────
-- 1. La mesure réelle fait foi.
UPDATE signals
   SET clv_pct = clv_pct_real
 WHERE clv_pct_real IS NOT NULL
   AND clv_pct IS DISTINCT FROM clv_pct_real;

-- 2. Pas de clv_pct_real, mais une clôture observée : on la recalcule.
UPDATE signals
   SET clv_pct = round((xbet_odd / closing_line - 1)::numeric * 100, 2)
 WHERE clv_pct_real IS NULL
   AND closing_line IS NOT NULL
   AND closing_line > 1.01
   AND xbet_odd IS NOT NULL;

-- ── ai_learning_ledger ───────────────────────────────────────────────
UPDATE ai_learning_ledger
   SET clv_final        = clv_pct_real,
       was_clv_positive = (clv_pct_real > 0)
 WHERE clv_pct_real IS NOT NULL
   AND clv_final IS DISTINCT FROM clv_pct_real;

UPDATE ai_learning_ledger
   SET clv_final        = round((odds / closing_pinnacle_price - 1)::numeric * 100, 2),
       was_clv_positive = (odds / closing_pinnacle_price - 1) > 0
 WHERE clv_pct_real IS NULL
   AND closing_pinnacle_price IS NOT NULL
   AND closing_pinnacle_price > 1.01
   AND odds IS NOT NULL;

-- Vérification attendue : les deux compteurs doivent valoir 0.
--
--   SELECT count(*) FROM signals
--    WHERE clv_pct IS NOT NULL AND clv_pct_real IS NOT NULL
--      AND clv_pct IS DISTINCT FROM clv_pct_real;
--   SELECT count(*) FROM ai_learning_ledger
--    WHERE clv_final IS NOT NULL AND clv_pct_real IS NOT NULL
--      AND clv_final IS DISTINCT FROM clv_pct_real;
