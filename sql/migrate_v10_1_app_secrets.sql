-- migrate_v10_1_app_secrets.sql — coffre à clés rotatives (v10.1)
--
-- POURQUOI : le plan OddsAPI est à 500 requêtes/mois et meurt en ~30 h
-- (voir core/odds_api.py). Changer de clé imposait jusqu'ici DEUX manips
-- manuelles à l'opérateur — secret GitHub Actions + variable d'env Vercel,
-- cette dernière n'ayant effet qu'après un Redeploy — pour une rotation
-- qui revient tous les 1-2 jours. Les deux se désynchronisaient : le
-- widget quota du dashboard affichait la clé Vercel (morte) pendant que
-- le moteur tournait sur celle de GitHub, ou l'inverse.
--
-- Cette table devient la source de vérité unique : core/secret_store.py la
-- lit en priorité et retombe sur os.environ si elle est vide/injoignable.
-- Une rotation = un UPDATE ici, plus aucun redéploiement.
--
-- SÉCURITÉ : RLS activé SANS AUCUNE POLICY. En Postgres/Supabase, RLS sans
-- policy = tout est refusé ; seul service_role, qui contourne RLS, peut
-- lire. La clé anon (celle que porte le dashboard en lecture) ne voit donc
-- jamais cette table. Le REVOKE explicite est une seconde barrière au cas
-- où une policy permissive serait ajoutée par erreur plus tard.

create table if not exists app_secrets (
    key        text primary key,
    value      text        not null,
    updated_at timestamptz not null default now(),
    note       text
);

alter table app_secrets enable row level security;

revoke all on table app_secrets from anon;
revoke all on table app_secrets from authenticated;

comment on table app_secrets is
    'Secrets rotatifs lus par core/secret_store.py. service_role uniquement (RLS sans policy).';
