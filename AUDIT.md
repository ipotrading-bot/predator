# AUDIT — PREDATOR (Phase 1, lecture seule, 2026-08-20)

## Cartographie

- **Stack** : Python 3.11, Flask (dashboard Vercel serverless), Supabase (PostgreSQL), GitHub Actions (11 workflows cron/manuels), pytest.
- **Points d'entrée** : `run_engine.py` (scan), `run_audit.py` (settlement), `run_wiz.py`, `run_rapport.py`, `run_closing_line.py`, `run_monte_carlo.py`, `backfill_ledger.py`, `api/index.py` (dashboard), `validator.py` (health-check manuel), `scripts/*` (outils ponctuels).
- **État général** : sain. **483 tests, 0 échec** (36 s). Aucune dépendance inutilisée (6/6 entrées de `requirements.txt` importées). Aucun import cassé, aucun cache/build committé, pas de node_modules. Repo 7,2 Mo dont ~1 Mo d'icônes PWA.

## SUPPRIMER (attente de validation)

| Item | Justification |
|---|---|
| `api/static/logo.jpg` (84 Ko) | Référencé nulle part — les templates utilisent `icons/icon-96.png` ; asset orphelin. |
| `.vercel-build-trigger` | Artefact ponctuel du 07/05 pour forcer un rebuild Vercel ; plus aucun rôle. |

C'est tout. Pas de code mort détecté : chaque module de `core/` est importé par au moins un point d'entrée ou test ; `validator.py` est un outil manuel documenté comme volontairement non importé.

## CORRIGER

| Item | Justification |
|---|---|
| 10 findings pyflakes (imports/variables inutilisés) | `core/ai_search.py:29` (json), `core/harvester.py:24` (SPORT_LABELS), `core/odds_api.py:359` (quota_remaining), `core/settlement.py:128` (scanned), `run_engine.py:445` (MIN_EDGE), `run_monte_carlo.py:10` (sys), `scripts/probe_xbet_sports.py:24`, `scripts/rank_sports.py:20`, `tests/test_odds_api_preflight.py:14`, `tests/test_tax_engine.py:14`. Trivial, zéro risque comportemental. |
| `.env.example` périmé | Sections 7/8/9/11 (`NEWS_API_KEY`, `PERPLEXITY_API_KEY`, `HISTORICAL_ODDS_KEY`, `BETTERSTACK_*`) et `PREDATOR_SECRET`/`API_SECRET_KEY` : **aucun code ne les lit**. Section 12 référence un `config.py` inexistant ; la checklist cite `pulse_hunter.yml` inexistant. `GITHUB_PAT`, lui, est bien lu (`api/index.py`). |
| Doc drift `.claude/skills/` | `predator-pipeline` et `predator-dashboard-check` affirment « no test suite / no automated tests » — faux depuis `tests/` (483 tests) + `tests.yml`. Une skill qui ment fait prendre de mauvaises décisions de diagnostic. |
| `validator.py` en-tête « v8.5 » | Cosmétique — le repo est en v10.x. |

## REFACTORER (optionnel — recommandation : ne pas toucher maintenant)

| Item | Justification |
|---|---|
| `run_engine.py` (1 840 lignes), `core/harvester.py` (977), `core/learning_layer.py` (941), `api/index.py` (901) | Gros mais couverts par les tests et stables ; découper un moteur qui gagne de l'argent pour un critère de taille = risque > gain. |
| Nav bar + `<head>` dupliqués dans les 6 templates | Un `base.html` Jinja éliminerait la duplication, mais chaque template a des variations de style volontaires (couleurs par page) ; à faire seulement si on retouche le dashboard de toute façon. |

## GARDER (faux positifs du template d'audit)

- `sql/migrate_*.sql` (18 fichiers) — migrations manuelles, seule trace du schéma ; jamais supprimer.
- `reports/edge_frequency_audit.md` — rapport d'analyse committé volontairement (généré par `scripts/edge_frequency_audit.py`).
- Icônes PWA (10 fichiers, ~1 Mo) — toutes listées dans `manifest.json`. Candidates à une **compression lossless en Phase 4** (icon-512 fait 432 Ko, ~4× trop lourd), pas à la suppression.
- Entrées `ui/.next` etc. dans `.gitignore` pour un dossier inexistant — inoffensif.
- `_SPORT_EMOJI`/`_SPORT_LABEL` superset dans `api/index.py` — documenté comme volontaire.

## Poids (top 10 hors .git)

icon-512.png 432 Ko · icon-384.png 248 Ko · run_engine.py 92 Ko · logo.jpg 84 Ko (orphelin) · icon-192.png 68 Ko · learning_layer.py 48 Ko · icon-152.png 44 Ko · predator.css 44 Ko · api/index.py 44 Ko · system.html 40 Ko.

## Phases suivantes — périmètre réel

- **Phase 2 (nettoyage)** : 2 fichiers à supprimer + toilettage `.env.example`. Rien à désinstaller.
- **Phase 3 (corrections)** : les 10 pyflakes + doc drift des skills. **Aucun bug fonctionnel identifié.** La gestion d'erreurs réseau existe déjà partout (politique « retourne [] sans crash », documentée et testée).
- **Phase 4 (perf)** : compression lossless des PNG (~600 Ko de gain estimé). Pas de bundle JS, pas de lazy loading applicable — stack Python/Jinja.
- **Phase 5 (setup Claude)** : **déjà en place à 80 %** — 2 skills projet, 1 subagent (`predator-diagnostician`), hooks (`dashboard_smoketest.sh`, `settings.json`). Manquant : un `CLAUDE.md` racine. Les 4 sub-agents génériques du template (code-reviewer, test-runner…) feraient doublon avec `/code-review`, le hook de smoke-test et `tests.yml` — déconseillé.
