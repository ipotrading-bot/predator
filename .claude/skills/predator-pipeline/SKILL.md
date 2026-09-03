---
name: predator-pipeline
description: Reference map of the PREDATOR PAIM data pipeline (odds ingestion → signal engine → Supabase → audit → learning layer → dashboard) and its known cross-file invariants. Use this BEFORE diagnosing "why is X empty/wrong/not updating" anywhere in this repo (run_engine.py, core/*, api/index.py, templates/*), before touching purge/audit/learning-layer logic, and before adding a new sport or Supabase column — it names the exact files that must stay in sync and the manual steps this stack does NOT automate.
---

# PREDATOR pipeline map

This project has no automated migration runner, and its test suite (`tests/`,
run by `ci.yml` on every push, which now gates the Vercel deploy) covers the maths and parsing logic but cannot
see live data or cron behaviour — to catch a *pipeline* break you still have to
trace the flow by hand. This skill is that trace, pre-done.

## La carte en dix lignes

1. **Ingestion** — Tier 1 OddsAPI (`core/odds_api.py`, rallumé le 2026-09-01,
   rythme mensuel) puis Tier 2 (`core/harvester.py` : odds-api.io,
   titan007, sources gratuites — api-sports retirée le 2026-09-03), enrichi par l'exchange Matchbook (sharp).
   Plus AUCUNE recherche web ni oracle LLM depuis le 2026-09-02.
2. **Signaux** — `run_engine.py` : devig (`core/math_engine.py`), edge = EV
   vraie au prix EXÉCUTABLE, consensus (`core/paim_engine.py`), écriture
   Supabase `signals` (`status='active'`).
3. **Purge** — en tête de chaque run, TOUJOURS scopée `status='active'` :
   une purge non scopée a déjà affamé le ledger des mois durant.
4. **Audit** (6 h) — settlement 100 % déterministe (`core/score_sources.py` :
   MLB statsapi, ESPN, TheSportsDB — ZÉRO IA), CLV, une ligne
   `ai_learning_ledger` par chemin réussi.
5. **Apprentissage** — `core/learning_layer.py` : seuils `meta.threshold_*`
   APPLIQUÉS au min_edge du scan (époque A6) ; verdicts loggés, jamais
   appliqués.
6. **Dashboard** — `api/index.py`, lecture seule, zone jouable 2-24 h.

## Les invariants qui cassent en silence

- **Sport-keys, 4 fichiers synchrones** : `SPORT_KEYS` (core/odds_api.py) ⇔
  `KELLY_FRACTION` (core/constants.py) ⇔ `SPORT_DEFAULTS`
  (core/learning_layer.py) ⇔ `SPORT_QUOTA`/`_SPORT_ORDER` (run_engine.py) —
  détail et gardiens dans [invariants.md](invariants.md).
- **Zone jouable 2-24 h** avant TOUTE analyse du ledger (hors d'elle, les
  conclusions s'inversent — détail dans
  [incidents_resumes.md](incidents_resumes.md)).
- **Époque de calibration** : aucune conclusion de seuil sur des lignes
  antérieures à la dernière correction (règle dure n°10).
- **Un run stérile sort en ÉCHEC** (`core/run_contract.py`) : un job vert
  qui n'a rien écrit est le mode de panne le plus coûteux du dépôt.
- **Budgets de sources** : rythme UNIQUE dans `core/daily_quota.py`
  (scans étalés, settlement JAMAIS étalé), rythme mensuel OddsAPI dans
  `core/scan_windows.py`.

## Quand lire quoi

| Question | Fichier |
|---|---|
| « Pourquoi 0 signal / d'où vient un prix / qui est sharp ? » — sources, line shopping, REPRICE, refonte EV | [flux.md](flux.md) |
| Ajouter un sport, une colonne, appliquer une migration, ce que le stack n'automatise PAS | [invariants.md](invariants.md) |
| « Le cron a-t-il tiré ? » — table des cadences, chien de garde Cloudflare, budget de déclenchements | [cadences_cron.md](cadences_cron.md) |
| Pannes déjà payées côté pipeline (10→20 août, quotas OddsAPI, pièges de la couche d'apprentissage) | [incidents_resumes.md](incidents_resumes.md) |
| Récit complet de TOUS les incidents | `INCIDENTS.md` (racine) |
| Invariant → test gardien | `AUDIT.md` §2 (racine) |

Premier réflexe avant tout diagnostic « pourquoi 0 signal » :
`python scripts/ops.py sources` (sonde sans dépenser), puis chercher dans
les logs les marqueurs en clair (`DÉPENSE |`, `LINESKIP`, `harvest SAUTÉ`,
`OddsAPI clé # écartée`, `AUDIT STÉRILE`).

## Ce que le stack n'automatise pas (résumé)

Les migrations SQL se collent À LA MAIN dans le SQL Editor Supabase ;
`backfill_ledger.py` est un `workflow_dispatch` one-shot ; un sandbox neuf
n'a ni credentials Supabase ni scope `workflow` sur son jeton GitHub —
vérifier avant de dire « fait ». Détail : [invariants.md](invariants.md).

## L'ancien sous-système d'analyse IA par match

Supprimé le 2026-08-26 sans archive (règle dure n°8 de `CLAUDE.md`, qui le
nomme) : ne pas le rechercher, ne pas le recréer. Mistral est depuis un
fournisseur ordinaire du registre (`core/ai_router.py`, lanes
filter/analyze), jamais validé par une inférence réelle —
`python scripts/ops.py ai` est le seul juge.
