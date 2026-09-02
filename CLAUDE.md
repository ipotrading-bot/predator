# PREDATOR PAIM

Pipeline de paris sportifs : ingestion de cotes → moteur de signaux (edge/devig)
→ Supabase → settlement/CLV → couche d'apprentissage → dashboard Flask (Vercel).
Calcul en crons GitHub Actions ; dashboard en lecture seule.

## Où est quoi

- `INCIDENTS.md` — **ce qui a déjà cassé, et pourquoi.** À LIRE AVANT DE
  DIAGNOSTIQUER, et avant de toucher sources, couche IA, workflows ou
  seuils : une règle dont on ignore la raison finit contournée.
- `AUDIT.md` — invariants → tests gardiens. À lire avant d'ajouter un sport,
  un fournisseur IA, une route ou un workflow.
- `.claude/rules/` — le DÉTAIL des règles, chargé par chemin (workflows,
  couche IA, dashboard, sql, apprentissage).
- `.claude/hooks/` — les règles dures en CODE (deny/ask mécaniques, lint,
  suite avant arrêt) ; README dedans, gardien `tests/test_claude_config.py`.
- Skills : `predator-pipeline` (carte du flux), `-add-sport`, `-migration`,
  `-ci-env`, `-incident`, `-release`, `-dashboard-check`.
- Sub-agents : `predator-diagnostician` (tout audit pipeline/santé),
  `test-runner`, `migration-author`, `ledger-analyst`, `incident-scribe`,
  `ci-log-digger`.

## Commandes

- Tests : `python -m pytest tests/ -q` (~40 s, doit rester à 0 échec).
- Lint : `python -m pyflakes $(git ls-files '*.py')`
- Dashboard local : skill `predator-dashboard-check` (mode démo)
- Comptes externes : `docs/actions_operateur.md`
- Piloter Supabase/Vercel : `python scripts/ops.py doctor|status|supabase …|vercel …`
  (credentials dans `.env`, gitignoré). `ops.py ai` fait un VRAI appel — seul
  diagnostic qui tranche sur un fournisseur IA. MCP Supabase épinglé et en
  LECTURE SEULE (`.mcp.json`).
- Pas de build. Le push ne déploie pas (déploiement Git Vercel DÉSACTIVÉ,
  `vercel.json`) : le job `deploy` de `ci.yml` pousse en CLI si la suite est
  verte.

## Architecture (fichiers clés)

- `run_engine.py` — scan + purge + émission des signaux (entrée principale)
- `core/odds_api.py` / `harvester.py` — sources de cotes (Tier 1/2)
- `core/math_engine.py` + `paim_engine.py` — devig, prix exécutable, edge, consensus
- `core/audit_engine.py` + `settlement.py` + `score_sources.py` — règlement
  (0 IA), CLV, ledger
- `core/learning_layer.py` — seuils (`meta.threshold_<sport>`, **APPLIQUÉS**
  au min_edge du scan, époque *A6*) ; verdicts loggés, jamais appliqués ;
  hebdo `scripts/weekly_report.py`
- `core/scan_windows.py` — fenêtres favorables (UTC) + politique de dépense
- `core/constants.py` — taxe, Kelly, `SCAN_TIMEOUTS`
- `core/run_contract.py` — un run qui n'a pas fait son travail sort en ÉCHEC
- `api/index.py` + `templates/*.html` — dashboard Flask
- `scripts/ci_env.py` — quels secrets atteignent quel job ;
  `scripts/ci_scan_mode.py` — quel cron donne quel mode de scan

## Conventions

- Python 3.11 en dev et en CI, code compatible 3.12 (build Vercel). Français
  dans les docstrings/commentaires, conventional commits.
- Les erreurs réseau/API ne crashent jamais : retour `[]` + log, documenté.
- Chaque changement de schéma = nouveau `sql/migrate_vX_Y.sql`, appliqué À LA
  MAIN dans le SQL Editor Supabase (aucun runner de migration).
- Tests purs uniquement (pas de réseau, pas de rendu de template).
- Dépendances VERROUILLÉES au `==`, transitives comprises
  (`requirements*.txt`). Ne jamais y remettre une borne molle.

## Règles dures — jamais à rediscuter

Détail dans `.claude/rules/` (chargé par chemin), justification dans
`INCIDENTS.md` (section citée), application mécanique dans `.claude/hooks/`.

1. ⛔ **JAMAIS `${{ toJSON(secrets) }}` dans un workflow** : GitHub refuse de
   le faire tourner, zéro job, aucun log — *Les blocs de secrets*.
2. Les blocs `env:` des workflows sont **générés** par `python scripts/ci_env.py
   --write`, posés par STEP — jamais écrits à la main.
3. Aucun nom de modèle IA en dur hors de `core/ai_router.py` — *Couche IA*.
4. `.python-version` vaut 3.12 et **appartient à Vercel** ; l'« aligner »
   casse le déploiement — *Deux interpréteurs*.
5. Une suite verte ne prouve **rien** sur le déploiement : skill
   `predator-release` après toute retouche de fichier de déploiement.
6. Ne **jamais** tenir à la main une liste qui existe ailleurs : dériver, ou
   test gardien — *Listes qui divergent*.
7. **Jamais un taux de réussite nu** : toujours Wilson + point mort après taxe.
8. Ne pas réintroduire Wiz (`/wiz`, `core/wiz_*`, `wiz_analysis`) — supprimé
   le 2026-08-26, sans archive.
9. Archiver, **jamais supprimer sèchement** des lignes de résultats (biais de
   survie).
10. Aucun seuil d'émission modifié sans mesure sur des lignes réglées
    POSTÉRIEURES à la correction en cours — *A6*.
11. `TAX_RATE`, `SHADOW_SPORTS` et le périmètre sportif = décisions
    opérateur, instruction explicite exigée dans la session courante —
    *TAX_RATE remis à 0.20 contre instruction*.
