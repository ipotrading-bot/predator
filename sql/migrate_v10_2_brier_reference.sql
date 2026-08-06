-- migrate_v10_2_brier_reference.sql — 2026-08-06
--
-- `brier_scores` existait depuis sa création sans qu'AUCUN code Python n'y
-- écrive : le Brier était recalculé à la volée pour /performance et jeté.
-- Aucune série temporelle n'existait donc pour voir une dérive de calibration
-- arriver, alors que c'est exactement ce qui a produit le second cliquet des
-- seuils (voir core/learning_layer.py:_calibration_flag).
--
-- Un Brier seul n'est pas interprétable : son plancher irréductible vaut
-- p(1-p) et dépend donc de la difficulté des paris (0,2500 à p=0,50 ; 0,2100 à
-- p=0,70). On persiste donc aussi sa RÉFÉRENCE — le Brier qu'obtiendrait un
-- modèle parfaitement calibré sur les mêmes probabilités annoncées — et
-- l'écart de calibration en points, qui est la mesure directement lisible.
--
-- Additive uniquement, idempotente. Aucune colonne existante n'est touchée.

ALTER TABLE brier_scores ADD COLUMN IF NOT EXISTS brier_reference   double precision;
ALTER TABLE brier_scores ADD COLUMN IF NOT EXISTS calibration_gap   double precision;
ALTER TABLE brier_scores ADD COLUMN IF NOT EXISTS scope             text;

COMMENT ON COLUMN brier_scores.brier_reference IS
  'Brier d''un modèle parfaitement calibré sur les mêmes probabilités annoncées (moyenne de p(1-p)). Le score brut ne se juge que par rapport à lui.';
COMMENT ON COLUMN brier_scores.calibration_gap IS
  'Probabilité moyenne annoncée moins taux de réussite réel, en fraction. Positif = surconfiant.';
COMMENT ON COLUMN brier_scores.scope IS
  'Périmètre mesuré : "playable" = zone 2-24h avant le coup d''envoi, la seule que le système recommande.';
