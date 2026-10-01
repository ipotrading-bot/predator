-- ============================================================================
-- migrate_v10_20_signaux_temps_reglementaire.sql — DEUX RECOMMANDATIONS DONT
-- L'EDGE COMPARAIT DEUX PARIS DIFFÉRENTS (2026-10-01)
--
-- OBJECTIF
-- --------
-- Retirer de la recommandation deux signaux ACTIFS émis avant le correctif
-- « 1xbet cote le hockey en temps réglementaire » (INCIDENTS.md) :
--
--   · 10632 — Vancouver Canucks vs Edmonton Oilers, NHL Over 6.5, 1xbet 2,04
--     contre Pinnacle 1,81, « +9,08 % ». Le total de 1xbet se règle sur les
--     60 minutes, celui de Pinnacle compte la prolongation : un 3-3 à la 60e
--     est Under chez l'un, Over chez l'autre. Match le 2026-10-02 à 02:10
--     UTC : tant que la ligne est active, le digest de 2 h et le dashboard
--     la proposent.
--   · 10604 — HC Slavia Prague vs HC Havirov Panthers (2e division tchèque),
--     moneyline Bet365 1,42, « +4,87 % ». Prix sharp = Smarkets à TROIS
--     issues (1,70 / nul / 4,80) dévigué sur deux : 73,9 % au lieu de ~70.
--     Admis comme « réglable » par l'homonyme Florida Panthers ; aucune de
--     nos sources ne règle cette ligue, la ligne faisait sortir l'audit en
--     STÉRILE (run 36849193517).
--
-- CE QUE CE SCRIPT NE TOUCHE PAS
-- ------------------------------
-- Aucune ligne RÉGLÉE (règle dure n°9). Les signaux de hockey déjà réglés
-- (10534, 10565, 10567) restent au ledger : leur ISSUE est juste dans les
-- deux lectures (Edmonton–Vancouver 5-5 à la 60e, Toronto–Montréal sans
-- prolongation), c'est leur edge d'entrée qui était faux — à écarter de
-- toute analyse d'edge du hockey, pas à effacer.
--
-- À APPLIQUER APRÈS LE DÉPLOIEMENT DU CORRECTIF : avant, le scan suivant
-- réémettrait 10632 (même match, même sélection, même prix).
--
-- Ce script ne DÉTRUIT rien : il DÉPLACE vers `signals_archive` puis retire
-- les seules lignes copiées, encore actives, dans la même transaction.
-- Idempotent (ON CONFLICT DO NOTHING ; une ligne déjà réglée ou purgée
-- entre-temps n'est pas touchée).
-- ============================================================================

BEGIN;

-- Colonnes NOMMÉES : `archived_at` est au MILIEU de l'archive (quatre
-- colonnes ont été ajoutées après elle) — une copie positionnelle `s.*`
-- se décalerait (motif v10_16).
INSERT INTO signals_archive
      (id, created_at, scanned_at, match, league, sport, xbet_odd,
       pinnacle_price, edge_pct, risk_flag, status, market, clv_pct,
       closing_line, closed_at, match_time, sharp_prob, market_key, match_id,
       selection_name, kelly_pct, advice, sharp_sources, consensus_score,
       outcome, closing_pinnacle_price, clv_pct_real, correlation_group,
       closing_captured_at, closing_source, archived_at, is_shadow,
       shadow_reason, soft_book, stake_xof)
SELECT id, created_at, scanned_at, match, league, sport, xbet_odd,
       pinnacle_price, edge_pct, risk_flag, status, market, clv_pct,
       closing_line, closed_at, match_time, sharp_prob, market_key, match_id,
       selection_name, kelly_pct, advice, sharp_sources, consensus_score,
       outcome, closing_pinnacle_price, clv_pct_real, correlation_group,
       closing_captured_at, closing_source, now(), is_shadow,
       shadow_reason, soft_book, stake_xof
FROM signals s
WHERE s.status = 'active'
  AND s.id IN (
    10632,  -- Vancouver Canucks vs Edmonton Oilers, NHL Over 6.5 @ 2.04 (1xbet) : total de temps réglementaire comparé à un total prolongation comprise
    10604   -- HC Slavia Prague vs HC Havirov Panthers, NHL ML @ 1.42 (bet365) : prix sharp à trois issues dévigué sur deux, ligue non réglable
  )
ON CONFLICT DO NOTHING;

DELETE FROM signals s
USING signals_archive a
WHERE s.id = a.id
  AND s.status = 'active'
  AND s.id IN (10632, 10604);

COMMIT;

-- ── RESTAURATION (seulement après vérification sur pièces) ────────────────
--   BEGIN;
--   INSERT INTO signals
--   SELECT <colonnes sauf archived_at> FROM signals_archive
--    WHERE id = <id>
--   ON CONFLICT DO NOTHING;
--   DELETE FROM signals_archive WHERE id = <id>;
--   COMMIT;

-- ── SELECT TÉMOINS après application ───────────────────────────────────────
-- (1) Les deux lignes sont en archive, telles qu'émises :
--   SELECT id, status, market, edge_pct FROM signals_archive WHERE id IN (10632, 10604);
--   -- attendu : 2 lignes, status = 'active'
-- (2) Plus aucune dans les signaux vivants :
--   SELECT count(*) FROM signals WHERE id IN (10632, 10604);
--   -- attendu : 0
-- (3) Le signal NHL comparable (moneyline Bet365, deux issues) est resté :
--   SELECT count(*) FROM signals WHERE id = 10629;
--   -- attendu : 1
