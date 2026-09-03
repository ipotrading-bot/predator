---
name: predator-ci-env
description: Comment les secrets atteignent les workflows PREDATOR — pools dérivés du registre IA, blocs env: GÉNÉRÉS, modes de scan par cron. Use when touching .github/workflows/*, adding an AI provider or a source key, or when a job seems to miss a credential.
---

# Secrets et environnements CI

## La règle mécanique

Les blocs `env:` des workflows sont **GÉNÉRÉS** par
`python scripts/ci_env.py --write`, jamais écrits à la main (règle dure
n°2), et posés **par STEP** — c'est ce qui garantit, lisiblement dans le
YAML, que le step REPRICE ne reçoit aucune clé payante. Les pools sont
DÉRIVÉS de `core.ai_router.REGISTRY` : ajouter un fournisseur au registre
suffit, le câblage suit. Le hook `guard_workflows.sh` rappelle cette règle à
tout Edit d'un bloc `env:`.

⛔ Règle dure n°1 : JAMAIS `${{ toJSON(secrets) }}` dans un workflow —
GitHub refuse de le faire tourner (conclusion `action_required`, ZÉRO job,
motif visible seulement sur la page HTML du run). Le hook le refuse ; le
récit est dans INCIDENTS.md, « Les blocs de secrets ».

## Procédure type (nouveau fournisseur / nouvelle clé)

1. Ajouter au bon endroit AMONT : `core/ai_router.py::REGISTRY` (fournisseur
   IA) ou le pool concerné dans `scripts/ci_env.py` (clé de source).
2. `python scripts/ci_env.py --write` — régénère les blocs de tous les
   workflows.
3. `python -m pytest tests/test_ci_env.py tests/test_workflow_secrets.py -q`
   — chaque bloc est comparé à son pool, tout secret nommé hors bloc généré
   échoue.
4. Poser la VALEUR du secret côté GitHub (`ops.py secrets-push` couvre le
   registre) — le câblage sans valeur est inoffensif mais inerte.

## Le mode d'un tick de scan

`scripts/ci_scan_mode.py::CRON_MODES` déduit le mode (`standard` ou
`reprice` — deux modes depuis le 2026-09-03) du cron qui a tiré ; `MODE_ENV`
pose `ODDS_API=1` pour standard et `REPRICE=1` pour reprice.
Un cron ajouté à `scan.yml` sans sa ligne fait échouer le run ET le test
(`test_la_table_cron_mode_est_exactement_les_crons_de_scan_yml`).

Qui reçoit quoi, en un coup d'œil : `python scripts/ci_env.py` (sans
`--write`) affiche les pools calculés.
