-- ============================================================================
-- migrate_v10_25_corrections_audit.sql — LES LIGNES FAUSSES TROUVÉES PAR
-- L'AUDIT GÉNÉRAL SONT CORRIGÉES (2026-10-29, instruction opérateur :
-- « corriger l'ensemble », sur la liste nommée 10963, 10072, 10184, fantômes)
--
-- CE QUE FAIT CETTE MIGRATION
-- ---------------------------
--   0. SAUVEGARDE de chaque valeur modifiée dans meta
--      `correction_archive_v10_25` (JSON) — rien ne se perd, tout se rejoue.
--   1. `time_to_match_minutes` de 64 lignes du ledger (signaux du 24/08 au
--      02/09) : calculé depuis `scanned_at` (dernier rafraîchissement) au lieu
--      de `created_at` (bug corrigé le 2026-09-03 pour les lignes NEUVES
--      seulement, core/db.log_to_ledger). Recalculé comme le code actuel :
--      troncature des minutes entre created_at et match_time.
--   2. `is_shadow` de 11 de ces lignes : le rattrapage v10_12 l'avait déduit
--      du délai faux (fantôme), alors que le pari était émis entre T-2h03 et
--      T-22h23 — recommandé. Le signal porte déjà false.
--   3. Signal 9824 : `is_shadow=true` posé par le rattrapage pour un pari émis
--      à T-2h00 exactement — la zone jouable inclut 120 min (learning_layer.
--      playable_rows, run_engine « < T-2h » fantôme). Le ledger porte false.
--   4. Signal 10963 (Commanders–Giants 27-27, NFL, RECOMMANDÉ) : LOSS → PUSH
--      au ledger et dans signals. Moneyline à deux issues, un nul rend la
--      mise (core/settlement.determine_outcome, corrigé le même jour).
--   5. Lignes 10072 (Dortmund–Villarreal senior, WIN) et 10184 (Giants–Padres
--      du 12/09 au soir, Over 7.5, WIN) RÉINSÉRÉES : refusées comme jumeaux
--      d'un AUTRE match homonyme par l'ancienne clé sans date
--      (core/db._ledger_jumeau_reel, corrigé le même jour). Champs recopiés du
--      signal comme le fait log_to_ledger ; created_at = date du règlement.
--
-- Aucune ligne supprimée (règle 9). Idempotent : chaque étape ne touche que
-- ce qui est encore faux ; la sauvegarde n'écrase pas une sauvegarde.
--
-- RESTAURATION : rejouer les valeurs de meta.correction_archive_v10_25 —
--   ledger (id → time_to_match_minutes, is_shadow, outcome), signals (id →
--   is_shadow, shadow_reason, outcome) ; pour les deux lignes réinsérées :
--   ledger WHERE signal_id IN (10072, 10184) AND created_at = l'heure de
--   règlement du signal, vers ai_learning_ledger_archive (modèle v10_5).
-- ============================================================================

-- 0. Sauvegarde AVANT toute modification.
INSERT INTO meta (key, value, updated_at)
SELECT 'correction_archive_v10_25', jsonb_build_object(
  'ledger', (SELECT coalesce(jsonb_agg(jsonb_build_object(
                 'id', l.id, 'signal_id', l.signal_id,
                 'time_to_match_minutes', l.time_to_match_minutes,
                 'is_shadow', l.is_shadow, 'outcome', l.outcome)), '[]'::jsonb)
               FROM ai_learning_ledger l JOIN signals s ON s.id = l.signal_id
              WHERE (s.match_time IS NOT NULL AND s.match_time <> ''
                     AND abs(l.time_to_match_minutes
                             - trunc(extract(epoch FROM (s.match_time::timestamptz - s.created_at)) / 60)) > 5)
                 OR l.signal_id = 10963),
  'signals', (SELECT jsonb_agg(jsonb_build_object(
                 'id', id, 'is_shadow', is_shadow, 'shadow_reason', shadow_reason,
                 'outcome', outcome))
                FROM signals WHERE id IN (9824, 10963))
)::text, now()
ON CONFLICT (key) DO NOTHING;

-- 1 + 2. Délai recalculé depuis la création ; drapeau fantôme aligné sur la
-- zone jouable quand le délai faux l'avait seul fait basculer.
UPDATE ai_learning_ledger l
   SET time_to_match_minutes = x.ttm,
       is_shadow = CASE WHEN l.is_shadow AND NOT s.is_shadow
                             AND x.ttm BETWEEN 120 AND 1440 THEN false
                        ELSE l.is_shadow END
  FROM signals s,
       LATERAL (SELECT trunc(extract(epoch FROM (s.match_time::timestamptz - s.created_at)) / 60)::int AS ttm) x
 WHERE s.id = l.signal_id
   AND s.match_time IS NOT NULL AND s.match_time <> ''
   AND abs(l.time_to_match_minutes - x.ttm) > 5;

-- 3. Signal 9824 : émis à T-2h00, dans la zone.
UPDATE signals SET is_shadow = false, shadow_reason = NULL
 WHERE id = 9824 AND is_shadow;

-- 4. 10963 : nul en moneyline à deux issues.
UPDATE ai_learning_ledger SET outcome = 'PUSH' WHERE signal_id = 10963 AND outcome = 'LOSS';
UPDATE signals            SET outcome = 'PUSH' WHERE id = 10963 AND outcome = 'LOSS';

-- 5. Réinsertion des deux résultats avalés par de faux jumeaux.
INSERT INTO ai_learning_ledger (
    signal_id, match, sport, league, market_type, market, selection, odds,
    time_to_match_minutes, initial_edge, clv_final, was_clv_positive, outcome,
    kelly_pct, closing_pinnacle_price, clv_pct_real, closing_captured_at,
    closing_source, sharp_prob, sharp_sources, consensus_score, is_shadow,
    soft_book, ouverture_sharp_prob, ouverture_cote, sharp_divergence_pts, created_at)
SELECT s.id, s.match, s.sport, s.league, s.market_key, s.market, s.selection_name, s.xbet_odd,
       trunc(extract(epoch FROM (s.match_time::timestamptz - s.created_at)) / 60)::int,
       s.edge_pct, NULL, NULL, s.outcome,
       s.kelly_pct, s.closing_pinnacle_price, s.clv_pct_real, s.closing_captured_at,
       s.closing_source, s.sharp_prob,
       CASE WHEN s.sharp_sources IS NULL OR s.sharp_sources = '' THEN NULL ELSE s.sharp_sources::jsonb END,
       round(s.consensus_score)::int, s.is_shadow,
       s.soft_book, s.ouverture_sharp_prob, s.ouverture_cote, s.sharp_divergence_pts,
       coalesce(s.closed_at, now())
  FROM signals s
 WHERE s.id IN (10072, 10184)
   AND s.outcome IN ('WIN', 'LOSS', 'PUSH')
   AND NOT EXISTS (SELECT 1 FROM ai_learning_ledger l WHERE l.signal_id = s.id);

-- Vérification :
-- SELECT count(*) FROM ai_learning_ledger l JOIN signals s ON s.id = l.signal_id
--  WHERE s.match_time <> '' AND abs(l.time_to_match_minutes
--        - trunc(extract(epoch FROM (s.match_time::timestamptz - s.created_at)) / 60)) > 5;   → 0
-- SELECT signal_id, outcome FROM ai_learning_ledger WHERE signal_id IN (10072, 10184, 10963);
--   → 10072 WIN, 10184 WIN, 10963 PUSH
-- SELECT count(*) FROM ai_learning_ledger l JOIN signals s ON s.id = l.signal_id
--  WHERE coalesce(l.is_shadow, false) <> coalesce(s.is_shadow, false);                    → 0
