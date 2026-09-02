# Invariants inter-fichiers — détail

> Référence de la skill `predator-pipeline`. Sections déplacées verbatim
> depuis SKILL.md (découpage du 2026-09-02), rien n'a été résumé.

## The sport-key invariant

These four places must list the exact same sport keys, or a sport silently gets
scanned but never learned-from (or vice versa):
- `core/odds_api.py` `SPORT_KEYS` (what's actually fetched) — the ground truth.
- `core/constants.py` `KELLY_FRACTION`.
- `core/learning_layer.py` `SPORT_DEFAULTS`.
- `run_engine.py` `SPORT_QUOTA` / `_SPORT_ORDER` (portfolio balancer).

`api/index.py`'s `_SPORT_EMOJI`/`_SPORT_LABEL`/`_DEFAULT_T` dicts (used by
`/ledger`) intentionally list a *superset* of display-only sports (tennis, mma,
darts, cricket, etc.) that are not currently harvested — that's harmless UI cruft,
not a bug, unless one of those keys starts appearing in real `signals` rows.


## Le recentrage sports du 2026-08-22 (mission « recentrage / quota / apprentissage »)

Détail complet : `reports/refonte_scope_2026-08.md`. Ce qu'il faut savoir pour diagnostiquer :
- **Retirés** : eSports, tennis de table, volleyball, handball (`RETIRED_SPORTS`,
  `core/constants.py`). Le garde vit dans `_emit` : aucun signal possible, même
  depuis un cache meta résiduel ou un slate REPRICE. Les fonctions de recherche
  web `fetch_esports_events`/`fetch_alternative_sports_batch`/`fetch_mma_events`
  N'EXISTENT PLUS. Lignes historiques conservées, settlement inchangé.
- **Plus aucun sport pricé par recherche web** : MMA et boxe (h2h) via
  `mma_mixed_martial_arts`/`boxing_boxing`, NFL (`americanfootball_nfl`, gardée
  par `SEASON_OPENS` — pas de présaison), LdC/UEL, Euroleague (sport-type
  `euroleague_basketball`, mécaniques basketball, Kelly 0.12). Le pré-vol rend 0
  hors saison/hors carte : l'ajout ne coûte rien.
- **L'invariant des 4 fichiers est désormais testé** (`tests/test_new_sports_phase2.py`,
  `tests/test_retired_sports.py`) : tout sport-type de `SPORT_KEYS` doit être
  dans `KELLY_FRACTION`, `SPORT_DEFAULTS`, `_QUOTA_FAST/_QUOTA_DEEP`, `SPORT_EMOJI`.
- **Politique de dépense OddsAPI** (`core/scan_windows.py`, injectée dans
  `fetch_odds`) : en fenêtre favorable → payé ; sport avec un signal actif à
  < 240 min du coup d'envoi → payé (closing line prioritaire) ; sinon 180 min mini
  entre deux scans payants d'une ligue, et sous `ODDS_API_RESERVE_CREDITS` (60) le
  fond s'espace. Chercher « DÉPENSE | » dans les logs pour savoir POURQUOI une
  ligue peuplée n'a pas été payée. `pool_remaining()` suit `x-requests-remaining`.
- **Verdicts par sport** (`meta.sport_verdict_<sport>`, posés par
  `compute_and_save` à chaque audit) : `promotion_eligible` (≥30 réglés, Wilson bas
  > rentabilité) / `perte_prouvee` / `non_demontre` → « retrait proposé ». Jamais
  appliqués : `KELLY_FRACTION` ne bouge que par commit.


## Manual steps this stack does NOT automate

- **Supabase schema changes** live in `sql/migrate_vX_Y.sql` but nothing runs them
  automatically — they must be pasted into the Supabase SQL Editor by a human with
  DB access. Check `sql/` for the latest unapplied migration before assuming a
  column exists.
- **`backfill_ledger.py`** (workflow: `.github/workflows/tools.yml`, input
  `backfill_ledger`) is
  `workflow_dispatch`-only, idempotent, one-shot. It re-populates
  `ai_learning_ledger` from historical terminal-status `signals` rows. Needed after
  any period where step 3's purge bug (or similar) silently dropped rows.
- Both of the above require credentials/permissions this agent does not have by
  default in a fresh sandbox (no Supabase URL/key, and the sandbox's `GITHUB_TOKEN`
  is an app-installation token without `workflow` scope — `gh workflow run` and
  `gh api .../dispatches` both 403). Don't assume a prior session's access
  persists; re-check with `env | grep -i supabase` and `gh auth status` rather than
  telling the user "done" on faith.
