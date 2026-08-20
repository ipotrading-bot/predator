# PREDATOR PAIM

Pipeline de paris sportifs : ingestion de cotes → moteur de signaux (edge/devig)
→ Supabase → settlement/CLV → couche d'apprentissage → dashboard Flask (Vercel).
Tout le calcul tourne en crons GitHub Actions ; le dashboard est en lecture seule.

## Commandes

- Tests : `python -m pytest tests/ -q` (~35 s, doit rester à 0 échec)
- Lint : `python -m pyflakes $(git ls-files '*.py')` (actuellement propre)
- Dashboard local : skill `predator-dashboard-check` (mode démo sans credentials)
- Piloter Supabase/Vercel : `python scripts/ops.py doctor|status|supabase …|vercel …`
  (credentials dans `.env`, gitignoré ; CLIs `supabase`/`vercel` aussi installables)
- Pas de build. Déploiement dashboard = push sur main (Vercel auto).

## Architecture (fichiers clés)

- `run_engine.py` — scan + purge + émission des signaux (entrée principale)
- `core/odds_api.py` / `harvester.py` / `oracle.py` — sources de cotes (Tier 1/2/3)
- `core/math_engine.py` + `paim_engine.py` — devig, edge, consensus
- `core/audit_engine.py` + `settlement.py` — règlement, CLV, remplit `ai_learning_ledger`
- `core/learning_layer.py` — seuils d'edge auto-ajustés (`meta.threshold_<sport>`)
- `run_wiz.py` + `core/wiz_*` — analyse contextuelle Mistral, écrit `wiz_analysis` UNIQUEMENT
- `api/index.py` + `templates/*.html` — dashboard Flask

## Conventions

- Python 3.11, français dans les docstrings/commentaires, conventional commits.
- Les erreurs réseau/API ne crashent jamais : retour `[]` + log, comportement documenté.
- Chaque changement de schéma = nouveau `sql/migrate_vX_Y.sql`, appliqué À LA MAIN
  dans le SQL Editor Supabase (aucun runner de migration).
- Tests purs uniquement (pas de réseau, pas de rendu de template) ; le rendu
  dashboard se vérifie via le hook/skill de smoke-test.

## Pièges connus (le détail vit dans les skills — les charger AVANT de diagnostiquer)

- Skill `predator-pipeline` : carte du flux, invariant des sport-keys (4 fichiers
  synchrones), règles de purge (`status='active'` obligatoire), cadences cron,
  quota OddsAPI, zone jouable 2-24h pour toute analyse du ledger.
- Wiz : jamais Groq/Tavily (domaine de panne séparé), jamais d'écriture hors
  `wiz_analysis`, poids Tier C négatif — gardé par `tests/test_wiz_engine.py`.
- Secrets : `core/secret_store.py` (table Supabase `app_secrets`) bat `os.environ` ;
  une valeur périmée dans la table gagne quand même.
- Sources de cotes : la règle est « authentifié par clé = joignable, sinon filtré
  par IP depuis les runners » (LineFeed/ESPN/SofaScore sont morts). Sharp =
  Matchbook (sans clé) + OddsAPI ; soft = api-sports. `ops.py sources` les sonde.
- OddsAPI = POOL de clés (`ODDS_API_KEYS`, bascule auto sur 401/422) ; une seule
  clé = dix jours sans signal quand elle meurt (août 2026). `rotate_odds_key.py --add`.
- Sub-agent `predator-diagnostician` pour tout audit pipeline/santé (isole les
  gros logs hors de la conversation principale).
