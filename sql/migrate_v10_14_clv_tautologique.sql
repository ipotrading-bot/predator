-- migrate_v10_14_clv_tautologique.sql — 2026-09-09 (soir)
--
-- POURQUOI
-- --------
-- Le commit b6e9937 a arrêté l'écriture d'un faux CLV (règle dure n°14 :
-- jamais de CLV sans un prix POSTÉRIEUR observé). Il n'a pas nettoyé ce qui
-- était déjà en base, et ces lignes ne s'effacent pas : elles remontent au
-- 2026-08-01. Mesuré juste après le déploiement :
--
--   · signals.clv_pct            : 414 renseignées, dont 210 SANS aucune
--                                  trace de clôture (ni closing_line, ni
--                                  clv_pct_real) ;
--   · ai_learning_ledger.clv_final : 420 dans le même cas sur 566.
--
-- Ces valeurs valent EXACTEMENT (xbet_odd / pinnacle_price - 1) * 100, soit
-- l'edge d'ENTRÉE — vérifié ligne à ligne avant d'écrire ce fichier. Elles
-- sont positives par construction (MIN_EDGE ne laisse jamais sortir un edge
-- négatif), et le dashboard les moyennait sous le libellé « CLV ».
--
-- RIEN N'EST PERDU (règle n°9)
-- ---------------------------
-- Aucune ligne de résultat n'est supprimée : seule une colonne qui n'a
-- jamais été une mesure repasse à NULL. L'écart de prix reste recalculable
-- depuis `xbet_odd` et `pinnacle_price`, qui restent sur la même ligne, et
-- l'edge du moteur reste dans `edge_pct` / `initial_edge`.
--
-- LE FILTRE EST LA PREUVE, PAS LA DATE
-- ------------------------------------
-- On n'efface QUE les lignes sans aucune trace d'observation postérieure.
-- Une ligne portant `closing_line` ou `clv_pct_real` a vu un vrai prix de
-- clôture : elle est conservée telle quelle, quelle que soit sa date. Cette
-- migration est donc idempotente — la rejouer ne touche plus rien.

-- 1. Le CLV affiché par /ledger et /audit.
UPDATE signals
   SET clv_pct = NULL
 WHERE clv_pct IS NOT NULL
   AND closing_line IS NULL
   AND clv_pct_real IS NULL;

-- 2. Le CLV du ledger permanent, et le verdict qui en était dérivé
--    (`was_clv_positive = clv > 0`, donc True sur 565 lignes sur 566).
UPDATE ai_learning_ledger
   SET clv_final        = NULL,
       was_clv_positive = NULL
 WHERE clv_final IS NOT NULL
   AND closing_pinnacle_price IS NULL
   AND clv_pct_real IS NULL;

-- Vérification attendue après application : les deux compteurs ci-dessous
-- doivent valoir 0, et les colonnes « réelles » être inchangées (81 lignes
-- avec closing_line, 132 avec clv_pct_real côté signals).
--
--   SELECT count(*) FROM signals
--    WHERE clv_pct IS NOT NULL AND closing_line IS NULL AND clv_pct_real IS NULL;
--   SELECT count(*) FROM ai_learning_ledger
--    WHERE clv_final IS NOT NULL AND closing_pinnacle_price IS NULL
--      AND clv_pct_real IS NULL;
