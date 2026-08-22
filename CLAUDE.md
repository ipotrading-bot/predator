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
- `core/learning_layer.py` — seuils d'edge auto-ajustés (`meta.threshold_<sport>`) + verdicts
  promotion/retrait par sport (`meta.sport_verdict_<sport>`, ≥30 réglés, Wilson vs rentabilité) —
  loggés, jamais appliqués ; rapport hebdo `scripts/weekly_report.py` (lundi 07:00 UTC)
- `core/scan_windows.py` — fenêtres favorables (UTC) + politique de dépense OddsAPI
  (180 min mini entre deux scans payants d'une ligue hors fenêtre, réserve de crédits ;
  fenêtres favorables et closing line jamais espacées ; chaque ligue sautée est loggée)
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
- Périmètre sports (2026-08-22) : eSports/tennis de table/volley/handball RETIRÉS
  (`RETIRED_SPORTS`, garde dans `_emit`, données historiques conservées) ; MMA/boxe/NFL/
  LdC/UEL/Euroleague sur flux OddsAPI réel (pré-vol 0 crédit, `SEASON_OPENS` pour la NFL).
  Plus aucun sport pricé par recherche web. Détail : `reports/refonte_scope_2026-08.md`.
- Sources gratuites Asie (mission 3, 2026-08-22) : cadre commun `core/source_adapter.py`
  (appariement par temps+ligue+STRUCTURE de cotes, jamais par nom ; divergence en
  POINTS de probabilité, pas en % relatif — un seuil relatif crie au loup sur tout
  outsider) ; `core/odds500.py` (odds.500.com, 30 books dont Pinnacle `cid=1055`,
  books identifiés par marge+pays car les noms sont masqués), `core/sevenm.py`
  (7M = source de NOMS anglais, pas de cotes — aucun endpoint de cotes gratuit),
  `core/prediction_markets.py` (Kalshi/Polymarket, rôle consensus).
  Nowgoal/win007 = MORTE depuis les runners (DNS), ne pas réessayer.
  Dictionnaire `team_aliases` (`sql/migrate_v10_3_team_aliases.sql`, À APPLIQUER) :
  clé = identifiant numérique de la source, pas le libellé ; un nom résolu ne
  repasse jamais par l'IA. Adaptateurs livrés mais PAS ENCORE CÂBLÉS dans run_engine.
- Pièges qui tuent une source en silence : User-Agent avec un accent → urllib encode
  en latin-1 → 403 Cloudflare (Polymarket) ; Kalshi rend `yes_bid`/`volume` à `null`
  et met les prix dans les champs `*_dollars` en chaîne ; un 1X2 amputé d'une patte
  devient indiscernable d'un moneyline et s'apparie avec lui.
- robots.txt : sur odds.500.com la QUERY STRING est la frontière (`/fenxi/ouzhi-*.shtml`
  autorisé nu, interdit avec `?ctype=`/`?order=`/`?cids=`). Ne jamais paramétrer un
  endpoint — gardé par `tests/test_odds500.py::TestRobotsTxt`.
- Sub-agent `predator-diagnostician` pour tout audit pipeline/santé (isole les
  gros logs hors de la conversation principale).
