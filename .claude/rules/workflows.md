---
paths:
  - ".github/**"
---

# Règles — workflows GitHub Actions

- ⛔ **Règle dure n°1** : JAMAIS `${{ toJSON(secrets) }}` dans un workflow.
  GitHub refuse de le faire tourner : conclusion `action_required`, ZÉRO
  job, aucun log — le motif n'apparaît que sur la page HTML du run. Cinq
  workflows sur six sont restés muets ainsi. Le hook `guard_workflows.sh`
  le refuse ; récit dans INCIDENTS.md, « Les blocs de secrets ». Même en
  DESCRIPTION d'une action composite : l'expression y est évaluée aussi.
- **Règle dure n°2** : les blocs `env:` sont GÉNÉRÉS par
  `python scripts/ci_env.py --write` depuis des pools DÉRIVÉS du registre
  IA, et posés par STEP (le step REPRICE ne reçoit aucune clé payante,
  lisible dans le YAML). Ne jamais les éditer à la main : modifier le pool,
  régénérer. Gardiens : `tests/test_ci_env.py` (chaque bloc comparé à son
  pool), `tests/test_workflow_secrets.py`.
- Ne jamais utiliser le contexte `inputs` nu dans un `if:` de job
  (`github.event.inputs.*` n'existe qu'en workflow_dispatch).
- Chaque cron de `scan.yml` doit avoir sa ligne dans
  `scripts/ci_scan_mode.py::CRON_MODES` — un cron sans sa ligne fait
  échouer le run ET le test.
- Chaque job porte une borne `timeout-minutes` ; Python des runners = 3.11,
  déclaré UNE fois dans `.github/actions/setup`.
- Détail et procédure : skill `predator-ci-env`.
