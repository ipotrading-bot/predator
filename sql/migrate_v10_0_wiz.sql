-- PREDATOR PAIM v10.0 — Migration WIZ (run once in Supabase SQL Editor)
--
-- Context (2026-07-23): WIZ est une couche d'analyse contextuelle par IA
-- (Mistral + Brave Search) qui enrichit les signaux actifs avec de la
-- recherche web. Sa mission première n'est PAS de classer — c'est de
-- détecter le FAUX EDGE : un edge élevé peut vouloir dire « le soft book
-- est lent » (vrai edge, on mise) ou « le soft book sait quelque chose »
-- (titulaire absent, lanceur MLB changé, équipe déjà qualifiée → piège).
--
-- Pourquoi une table séparée et pas des colonnes sur `signals` :
-- l'edge de PREDATOR est quantitatif et validé (asymétrie Pinnacle/soft
-- book) ; les données qualitatives que Wiz collecte sont statistiquement
-- perdantes en moyenne quand on les laisse toucher au calcul. Wiz n'écrit
-- donc AUCUNE colonne de `signals` — la séparation physique des tables est
-- la garantie mécanique de cet invariant, pas juste une convention de code.
--
-- Pourquoi la clé est `match_id` et pas `signal_id` : Brave Search est le
-- goulot d'étranglement (~2 000 requêtes/mois en free tier), pas le LLM.
-- Un même match génère souvent 3 signaux (h2h + totals + spreads) qui
-- partagent exactement le même contexte terrain (compositions, blessures,
-- météo) — une seule analyse les couvre tous, et `signal_ids` garde la
-- trace de ceux qui étaient actifs au moment de l'analyse.
--
-- `verdict` et `wiz_confidence` sont stockés dès maintenant même si
-- WIZ_ENFORCE=0 (mode observation, aucun blocage) : c'est ce qui rendra
-- possible, dans ~30 signaux réglés, de mesurer rétroactivement via le
-- Brier score de core/learning_layer.py si Wiz apporte réellement de
-- l'information avant de lui donner le moindre pouvoir de veto.
--
-- Cette migration est idempotente : CREATE TABLE/INDEX/POLICY IF NOT
-- EXISTS sont des no-op si déjà appliqués. Elle ne fait RIEN tant qu'elle
-- n'est pas collée à la main dans le SQL Editor Supabase — la committer
-- dans git ne l'applique PAS à la base.

CREATE TABLE IF NOT EXISTS public.wiz_analysis (
  id              bigserial PRIMARY KEY,
  created_at      timestamptz DEFAULT now(),
  analyzed_at     timestamptz,
  match_id        text,
  match           text NOT NULL,
  sport           text,
  league          text,
  signal_ids      jsonb,        -- signaux couverts par cette analyse (cache match-level)
  verdict         text,         -- CONFIRME | NEUTRE | ALERTE | VETO | INDISPONIBLE
  wiz_confidence  numeric(5,2), -- 0-100
  wiz_rank_score  numeric(8,4), -- score composite de classement
  arguments       jsonb,        -- [{texte, source_url, tier, direction, poids}]
  red_flags       jsonb,        -- [{texte, source_url, severite}]
  resume          text,
  sources_count   int,
  model_used      text,
  queries_used    int,
  UNIQUE (match_id, analyzed_at)
);

-- La page /wiz et run_wiz.py lisent tous les deux « la dernière analyse par
-- match » — c'est le seul motif d'accès réel de cette table.
CREATE INDEX IF NOT EXISTS wiz_analysis_match_analyzed_idx
  ON public.wiz_analysis (match_id, analyzed_at DESC);

-- ============================================================
-- RLS — même motif que sql/migrate_v9_3_tighten_rls.sql
-- ============================================================
-- Lecture anon (la page /wiz sur Vercel lit avec SUPABASE_KEY),
-- écriture réservée au service_role (run_wiz.py en GitHub Actions).
-- Contrairement à `signals`, cette table naît directement avec le modèle
-- strict : aucune policy allow_all_insert n'a jamais existé ici, il n'y a
-- donc rien à révoquer.
ALTER TABLE public.wiz_analysis ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_policies
                 WHERE tablename = 'wiz_analysis' AND policyname = 'wiz_read') THEN
    CREATE POLICY "wiz_read" ON public.wiz_analysis FOR SELECT USING (true);
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_policies
                 WHERE tablename = 'wiz_analysis' AND policyname = 'service_role_insert') THEN
    CREATE POLICY "service_role_insert" ON public.wiz_analysis
      FOR INSERT TO service_role WITH CHECK (true);
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_policies
                 WHERE tablename = 'wiz_analysis' AND policyname = 'service_role_update') THEN
    CREATE POLICY "service_role_update" ON public.wiz_analysis
      FOR UPDATE TO service_role USING (true) WITH CHECK (true);
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_policies
                 WHERE tablename = 'wiz_analysis' AND policyname = 'service_role_delete') THEN
    CREATE POLICY "service_role_delete" ON public.wiz_analysis
      FOR DELETE TO service_role USING (true);
  END IF;
END $$;

-- Pas de purge automatique ici, volontairement : la table est petite (une
-- ligne par match analysé, ~40 max par run) et l'historique complet est
-- exactement ce dont on aura besoin pour corréler wiz_confidence aux
-- outcomes réels du ledger. Voir aussi run_engine.py `_purge_old_signals`
-- (2026-07-xx) : une purge trop agressive avait déjà silencieusement affamé
-- ai_learning_ledger pendant des mois — on ne refait pas cette erreur.

-- ============================================================
-- Post-migration check (à lancer à la main, hors migration)
-- ============================================================
-- SELECT column_name, data_type FROM information_schema.columns
-- WHERE table_name = 'wiz_analysis' ORDER BY ordinal_position;
--
-- SELECT policyname, cmd, roles FROM pg_policies WHERE tablename = 'wiz_analysis';
--
-- SELECT indexname FROM pg_indexes WHERE tablename = 'wiz_analysis';
