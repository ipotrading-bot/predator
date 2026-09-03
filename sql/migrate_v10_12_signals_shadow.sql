-- ============================================================================
-- migrate_v10_12_signals_shadow.sql — LE FANTÔME DEVIENT UNE COLONNE (2026-09-03)
--
-- OBJECTIF
-- --------
-- Les signaux FANTÔMES (mode golden hour, < T-2h : mesurés, réglés, appris,
-- mais JAMAIS recommandés — run_engine.SHADOW_GOLDEN_HOUR) étaient persistés
-- dans `signals` en status 'active' SANS AUCUN MARQUEUR. Le dashboard (/ et
-- /api/signals) sélectionne `status = 'active'` : il les affichait donc comme
-- des paris à poser, alors que Telegram ne les envoyait pas. L'opérateur
-- pariait dessus depuis le dashboard.
--
-- MESURÉ le 2026-09-03 (signaux scannés depuis le 2026-09-01) :
--   · 27 actifs, dont 17 émis à moins de 120 min du coup d'envoi ;
--   · 22 réglés, dont 19 fantômes (8 gagnés – 11 perdus) contre 3 recommandés.
--
-- CE QUE FAIT CETTE MIGRATION
-- ---------------------------
--   1. `signals.is_shadow boolean NOT NULL DEFAULT false` + `shadow_reason text`
--      — posés par run_engine._save à l'émission (partition AVANT persistance),
--      JAMAIS modifiés par un rafraîchissement (une recommandation envoyée ne
--      devient pas fantôme parce qu'un tick golden l'a revue à T-1h).
--   2. `ai_learning_ledger.is_shadow` — recopié du signal au règlement
--      (core/db.log_to_ledger), pour que /performance n'ait plus à DEVINER le
--      fantôme depuis `time_to_match_minutes`.
--   3. Mêmes colonnes sur les deux archives (`LIKE` figé à v10_5) : un futur
--      `INSERT INTO …_archive SELECT *` échouerait sinon sur le nombre de
--      colonnes.
--   4. BACKFILL, pas de suppression (règle dure n°9) : toute ligne émise à
--      moins de 120 min du coup d'envoi — la borne de la zone jouable,
--      core/learning_layer._PLAYABLE_MIN_MINUTES — est marquée fantôme.
--      Sur `signals`, la distance se calcule depuis `created_at` (première
--      insertion, jamais réécrite) et NON `scanned_at`, que chaque
--      rafraîchissement écrase : 309 lignes ont un scanned_at postérieur à
--      leur created_at, et une recommandation de T-6h revue à T-1h par le tick
--      golden aurait été marquée fantôme à tort. Sur le ledger, seule
--      `time_to_match_minutes` existe (calculée depuis scanned_at jusqu'à ce
--      correctif) : le backfill hérite de ce biais pour les lignes anciennes,
--      c'est dit, et les lignes nouvelles portent le vrai drapeau.
--
-- Idempotent : ADD COLUMN IF NOT EXISTS ; les UPDATE ne touchent que les
-- lignes encore à false qui remplissent le critère.
--
-- À APPLIQUER À LA MAIN (SQL Editor ou `python scripts/ops.py supabase migrate
-- sql/migrate_v10_12_signals_shadow.sql`) AVANT de déployer api/index.py :
-- le dashboard filtre sur `is_shadow`, et PostgREST refuse un filtre sur une
-- colonne absente (la page tomberait en état vide).
-- ============================================================================

ALTER TABLE signals
  ADD COLUMN IF NOT EXISTS is_shadow     boolean NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS shadow_reason text;

ALTER TABLE ai_learning_ledger
  ADD COLUMN IF NOT EXISTS is_shadow boolean NOT NULL DEFAULT false;

ALTER TABLE signals_archive
  ADD COLUMN IF NOT EXISTS is_shadow     boolean NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS shadow_reason text;

ALTER TABLE ai_learning_ledger_archive
  ADD COLUMN IF NOT EXISTS is_shadow boolean NOT NULL DEFAULT false;

-- Backfill signals : < 120 min entre la PREMIÈRE insertion et le coup d'envoi.
UPDATE signals
   SET is_shadow = true,
       shadow_reason = 'backfill_t_minus_2h'
 WHERE is_shadow = false
   AND match_time IS NOT NULL
   AND extract(epoch FROM (match_time::timestamptz - created_at)) / 60 < 120;

-- Backfill ledger : la seule distance connue (voir en-tête pour son biais).
UPDATE ai_learning_ledger
   SET is_shadow = true
 WHERE is_shadow = false
   AND time_to_match_minutes IS NOT NULL
   AND time_to_match_minutes < 120;

-- Le dashboard lit signals avec la clé anon : la colonne suit la table, rien
-- à accorder de plus. Vérification attendue après application :
--   select is_shadow, status, count(*) from signals group by 1,2 order by 1,2;
