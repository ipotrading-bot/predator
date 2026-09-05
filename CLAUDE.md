# PREDATOR PAIM

Pipeline de paris sportifs : ingestion de cotes → moteur de signaux (edge/devig)
→ Supabase → settlement/CLV → couche d'apprentissage → dashboard Flask (Vercel).
Calcul en crons GitHub Actions ; dashboard en lecture seule.

## Où est quoi

- `INCIDENTS.md` — **ce qui a déjà cassé, et pourquoi.** À LIRE AVANT DE
  DIAGNOSTIQUER, et avant de toucher sources, couche IA, workflows ou
  seuils : une règle sans sa raison finit contournée.
- `docs/systeme_de_scan.md` — le scan en 2 modes, pour l'opérateur.
- `AUDIT.md` — invariants → tests gardiens, à lire avant tout ajout (sport,
  fournisseur IA, route, workflow).
- `.claude/rules/` — le DÉTAIL des règles, chargé par chemin.
- `.claude/hooks/` — les règles dures en CODE ; README dedans, gardien
  `tests/test_claude_config.py`.
- Skills et sub-agents : `.claude/skills/`, `.claude/agents/` (pas de liste
  ici, règle 6). Commencer par `predator-pipeline`, la carte du flux.

## Commandes

- Tests : `python -m pytest tests/ -q` (~10 s, 0 échec).
- Lint : `python -m pyflakes $(git ls-files '*.py')`
- Dashboard local : skill `predator-dashboard-check`
- Comptes externes : `docs/actions_operateur.md`
- Piloter Supabase/Vercel : `python scripts/ops.py doctor|status|supabase …|vercel …`
  (credentials gitignorés). `ops.py ai` fait un VRAI appel — seul diagnostic
  qui tranche sur un fournisseur IA. MCP Supabase en LECTURE SEULE
  (`.mcp.json`).
- Pas de build. Le push ne déploie pas (déploiement Git Vercel DÉSACTIVÉ,
  `vercel.json`) : le job `deploy` de `ci.yml` pousse en CLI si la suite est verte.

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
- Schéma : un `sql/migrate_vX_Y.sql` par changement, appliqué par
  `ops.py supabase migrate <f>` ou à la main. Rien ne les rejoue.
- Tests purs uniquement (pas de réseau, pas de rendu de template).
- Dépendances VERROUILLÉES au `==`, transitives comprises
  (`requirements*.txt`), jamais de borne molle.

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
8. Ne pas réintroduire Wiz (`/wiz`, `core/wiz_*`) — supprimé le 2026-08-26.
9. Archiver, **jamais supprimer sèchement** des lignes de résultats (biais de
   survie).
10. Aucun seuil d'émission modifié sans mesure sur des lignes réglées
    POSTÉRIEURES à la correction en cours — *A6*.
11. `TAX_RATE`, `SHADOW_SPORTS` et le périmètre sportif = décisions
    opérateur, instruction explicite exigée dans la session courante —
    *TAX_RATE remis à 0.20 contre instruction*.
12. ⛔ Cadence d'un workflow multi-modes : surveiller le MODE (`run-name`)
    et le CRÉNEAU DÛ, jamais la fraîcheur du fichier — *Le chien de garde
    surveillait un FICHIER, pas un MODE*.
13. Une source n'entre qu'avec budget chiffré, critère de retrait daté,
    test gardien, même commit — `AUDIT.md` §3bis.
