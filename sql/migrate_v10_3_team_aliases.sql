-- migrate_v10_3_team_aliases.sql — 2026-08-22
--
-- DICTIONNAIRE D'ALIAS D'ÉQUIPES — pas un service de traduction.
--
-- Les nouvelles sources gratuites (odds.500.com en tête) nomment les équipes
-- en chinois : « 鹿岛鹿角 » pour Kashima Antlers, « 曼彻斯特联 » pour Manchester
-- United. Traduire à chaque rencontre coûterait un appel IA par match et par
-- run — plusieurs milliers par mois pour un ensemble de noms qui, lui, ne
-- change qu'au rythme des promotions/relégations.
--
-- D'où une table : un nom inconnu est résolu UNE fois, écrit ici, et réutilisé
-- à vie. Le dictionnaire se construit ; il ne se retraduit jamais.
--
-- POURQUOI `source_team_id` EST LA VRAIE CLÉ
-- ------------------------------------------
-- odds.500.com expose un identifiant numérique stable par équipe
-- (`liansai.500.com/team/1029/`) et 7M en expose un autre (`taid`). Ces
-- identifiants n'ont PAS de langue : ils survivent à un changement de graphie,
-- à une abréviation, à un nom de sponsor. Le libellé `alias_source` n'est
-- gardé que pour la lisibilité humaine et le repli quand la source ne publie
-- pas d'identifiant. C'est la transposition, aux équipes, de la règle
-- d'appariement du moteur : la structure d'abord, le nom en confirmation.
--
-- CONFIANCE ET INVALIDATION
-- -------------------------
-- `confidence` monte à chaque fois qu'un appariement indépendant (fenêtre de
-- coup d'envoi + ligue + proximité des cotes) confirme l'alias, et tombe à 0
-- dès qu'un appariement le contredit. Un alias sous le seuil n'est pas
-- « à revoir » : le match qui en dépend est écarté, sans signal. Un faux
-- appariement d'équipes est la seule erreur de ce pipeline qui produise un
-- edge élevé, crédible et entièrement imaginaire — voir le plafond
-- MAX_SOFT_OUTLIER de core/titan007.py, qui traite le même risque côté prix.
--
-- Additive uniquement, idempotente. Aucune table existante n'est touchée.

CREATE TABLE IF NOT EXISTS team_aliases (
  id              bigserial PRIMARY KEY,
  source          text        NOT NULL,
  alias_source    text        NOT NULL,
  source_team_id  text,
  lang            text        NOT NULL DEFAULT 'zh',
  canonical_name  text        NOT NULL,
  league          text,
  confidence      double precision NOT NULL DEFAULT 0.5,
  hits            integer     NOT NULL DEFAULT 0,
  contradictions  integer     NOT NULL DEFAULT 0,
  resolved_by     text,
  verified_at     timestamptz,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now()
);

-- Un alias est unique DANS SA SOURCE : « 国米 » chez 500.com et « 国米 » chez
-- 7M sont deux entrées, parce que rien ne garantit qu'elles désignent la même
-- équipe tant qu'un appariement ne l'a pas confirmé.
CREATE UNIQUE INDEX IF NOT EXISTS team_aliases_source_alias_uidx
  ON team_aliases (source, alias_source, COALESCE(league, ''));

-- Chemin de lecture principal : identifiant numérique de la source.
CREATE INDEX IF NOT EXISTS team_aliases_source_team_id_idx
  ON team_aliases (source, source_team_id) WHERE source_team_id IS NOT NULL;

-- Chemin inverse : « quels alias pointent vers ce nom canonique ? »
CREATE INDEX IF NOT EXISTS team_aliases_canonical_idx
  ON team_aliases (lower(canonical_name));

COMMENT ON TABLE team_aliases IS
  'Dictionnaire persistant nom source (CJK ou autre) -> nom canonique anglais. Écrit une fois par nom, jamais retraduit.';
COMMENT ON COLUMN team_aliases.source IS
  'Adaptateur qui a vu ce libellé : odds500, sevenm, titan007…';
COMMENT ON COLUMN team_aliases.alias_source IS
  'Libellé BRUT tel que publié par la source (« 鹿岛鹿角 »).';
COMMENT ON COLUMN team_aliases.source_team_id IS
  'Identifiant numérique stable de l''équipe chez la source — sans langue, prioritaire sur le libellé.';
COMMENT ON COLUMN team_aliases.lang IS
  'Langue détectée du libellé : zh, ja, ko, en… Sert à choisir le chemin de résolution, pas à traduire.';
COMMENT ON COLUMN team_aliases.canonical_name IS
  'Nom anglais canonique utilisé par le moteur et le ledger.';
COMMENT ON COLUMN team_aliases.confidence IS
  '0..1. Monte à chaque appariement indépendant confirmé, tombe à 0 sur contradiction. Sous le seuil, le match est écarté.';
COMMENT ON COLUMN team_aliases.resolved_by IS
  'Chemin de résolution : sevenm (dictionnaire gratuit), ai (Groq, payant), manual.';
COMMENT ON COLUMN team_aliases.verified_at IS
  'Dernier appariement indépendant ayant confirmé cet alias.';

ALTER TABLE team_aliases ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_policies
                 WHERE tablename = 'team_aliases' AND policyname = 'team_aliases_read') THEN
    CREATE POLICY team_aliases_read ON team_aliases FOR SELECT USING (true);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_policies
                 WHERE tablename = 'team_aliases' AND policyname = 'team_aliases_write') THEN
    CREATE POLICY team_aliases_write ON team_aliases FOR ALL
      TO service_role USING (true) WITH CHECK (true);
  END IF;
END $$;
