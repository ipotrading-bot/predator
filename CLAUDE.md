# PREDATOR PAIM

Pipeline de paris sportifs : ingestion de cotes → moteur de signaux (edge/devig)
→ Supabase → settlement/CLV → couche d'apprentissage → dashboard Flask (Vercel).
Calcul en crons GitHub Actions ; dashboard en lecture seule.

## Où est quoi

- `INCIDENTS.md` — **ce qui a déjà cassé, et pourquoi.** À LIRE AVANT DE
  DIAGNOSTIQUER, et avant de toucher sources, couche IA, workflows ou
  seuils : une règle dont on ignore la raison finit contournée.
- `AUDIT.md` — carte des invariants et de leurs tests gardiens. À lire avant
  d'ajouter un sport, un fournisseur IA, une route ou un workflow.
- Skill `predator-pipeline` — carte du flux, invariant des sport-keys
  (4 fichiers synchrones), purge (`status='active'` obligatoire), cadences
  cron, zone jouable 2-24 h pour toute analyse du ledger.
- Sub-agent `predator-diagnostician` — tout audit pipeline/santé (isole les
  gros logs).

## Commandes

- Tests : `python -m pytest tests/ -q` (~40 s, doit rester à 0 échec).
- Lint : `python -m pyflakes $(git ls-files '*.py')`
- Dashboard local : skill `predator-dashboard-check` (mode démo)
- Comptes externes : `docs/actions_operateur.md`
- Piloter Supabase/Vercel : `python scripts/ops.py doctor|status|supabase …|vercel …`
  (credentials dans `.env`, gitignoré ; CLIs `supabase`/`vercel` aussi
  installables). `ops.py ai` fait un VRAI appel — seul diagnostic qui tranche
  sur un fournisseur IA.
- Pas de build. Le push ne déploie pas (déploiement Git Vercel DÉSACTIVÉ,
  `vercel.json`) : le job `deploy` de `ci.yml` pousse en CLI si la suite est verte.

## Architecture (fichiers clés)

- `run_engine.py` — scan + purge + émission des signaux (entrée principale)
- `core/odds_api.py` / `harvester.py` — sources de cotes (Tier 1/2)
- `core/math_engine.py` + `paim_engine.py` — devig, prix exécutable, edge, consensus
- `core/audit_engine.py` + `settlement.py` + `score_sources.py` — règlement
  (0 IA), CLV, ledger
- `core/learning_layer.py` — seuils (`meta.threshold_<sport>`, **APPLIQUÉS**
  au min_edge du scan, époque *A6*) ; verdicts (≥30 réglés, Wilson vs
  rentabilité) loggés, jamais appliqués ; hebdo `scripts/weekly_report.py`
- `core/scan_windows.py` — fenêtres favorables (UTC) + politique de dépense
- `core/constants.py` — taxe, Kelly, `SCAN_TIMEOUTS` (budget par mode de scan)
- `core/run_contract.py` — un run qui n'a pas fait son travail sort en ÉCHEC
- `api/index.py` + `templates/*.html` — dashboard Flask
- `scripts/ci_env.py` — quels secrets atteignent quel job (pools dérivés du
  registre IA) ; `scripts/ci_scan_mode.py` — quel cron donne quel mode de scan

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

Chacune a coûté une panne. Le récit et la justification sont dans
`INCIDENTS.md`, à la section nommée.

1. ⛔ **JAMAIS `${{ toJSON(secrets) }}` dans un workflow.** GitHub refuse de
   le faire tourner : zéro job, aucun log — *Les blocs de secrets*.
2. Les blocs `env:` des workflows sont **générés** par `python scripts/ci_env.py
   --write`, jamais écrits à la main, et posés par STEP.
3. Ne **jamais** coder un nom de modèle IA en dur hors de `core/ai_router.py` —
   *Couche IA*.
4. `.python-version` vaut 3.12 et **appartient à Vercel**. L'« aligner » sur
   3.11 casse le déploiement — *Deux interpréteurs*.
5. Une suite verte ne prouve **rien** sur le déploiement. Après toute retouche
   de `vercel.json`, `.python-version`, `requirements*.txt` ou `api/index.py` :
   `ops.py vercel deployments` puis `curl …/api/health`.
6. Ne **jamais** tenir à la main une liste qui existe ailleurs : on la dérive,
   ou un test la compare à sa source — *Listes qui divergent*.
7. **Jamais un taux de réussite nu** : toujours Wilson + point mort après taxe.
8. Ne pas réintroduire Wiz (`/wiz`, `core/wiz_*`, `wiz_analysis`) — supprimé le
   2026-08-26, sans archive.
9. Archiver, **jamais supprimer sèchement** des lignes de résultats : seule
   trace empirique, les ignorer crée un biais de survie.
10. Aucun seuil numérique d'émission n'est modifié sans mesure sur des lignes
    réglées POSTÉRIEURES à la correction en cours — *A6*.
11. `TAX_RATE`, `SHADOW_SPORTS` et le périmètre sportif sont des décisions
    opérateur ; ne pas les modifier sans instruction explicite dans la
    session courante — *TAX_RATE remis à 0.20 contre instruction*.
