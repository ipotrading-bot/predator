# PREDATOR PAIM

Pipeline de paris sportifs : ingestion de cotes → moteur de signaux (edge/devig)
→ Supabase → settlement/CLV → couche d'apprentissage → dashboard Flask (Vercel).
Tout le calcul tourne en crons GitHub Actions ; le dashboard est en lecture seule.

## Commandes

- Tests : `python -m pytest tests/ -q` (~45 s, 1033 tests, doit rester à 0 échec)
- Carte des invariants et de leurs gardiens : `AUDIT.md` (à lire avant
  d'ajouter un sport, un fournisseur IA, une route ou un workflow)
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
  Dictionnaire `team_aliases` (`sql/migrate_v10_3_team_aliases.sql`) :
  clé = identifiant numérique de la source, pas le libellé ; un nom résolu ne
  repasse jamais par l'IA. Migration APPLIQUÉE le 2026-08-22 — vérifié en base
  le même jour (table présente, 12 lignes). L'ancienne mention « À APPLIQUER »
  contredisait la ligne suivante ; une consigne qui se contredit fait rejouer
  une migration déjà passée.
  Câblage : `core/free_sources.py` (appelé EN DERNIER par harvester.fetch_matches,
  car il se mesure contre les sources déjà collectées). odds500 démarre en MODE
  OMBRE → rend [] tant qu'il n'a pas 100 matchs appariés à <2 pts de divergence :
  zéro signal au premier déploiement, c'est voulu. Coupe-circuit `FREE_SOURCES=0`.
  Un match dont une équipe ne se résout pas est ÉCARTÉ, jamais émis en chinois.
  Curseur `meta.sevenm_sitemap_cursor` OBLIGATOIRE : le sitemap 7M (936 ids)
  commence par des coupes mineures sans recoupement — sans curseur, 0 alias
  appris à chaque run et branchement inerte en silence (constaté en live).
- Pièges qui tuent une source en silence : User-Agent avec un accent → urllib encode
  en latin-1 → 403 Cloudflare (Polymarket) ; Kalshi rend `yes_bid`/`volume` à `null`
  et met les prix dans les champs `*_dollars` en chaîne ; un 1X2 amputé d'une patte
  devient indiscernable d'un moneyline et s'apparie avec lui ; la `<description>`
  d'un item Google News RSS est le TITRE recopié, pas un extrait — une source qui
  « répond » peut ne porter aucun fait (100% d'INDISPONIBLE sur /wiz, 2026-08-22 ;
  Bing News RSS ajouté pour les vrais extraits, `core/wiz_sources.py`).
- robots.txt : sur odds.500.com la QUERY STRING est la frontière (`/fenxi/ouzhi-*.shtml`
  autorisé nu, interdit avec `?ctype=`/`?order=`/`?cids=`). Ne jamais paramétrer un
  endpoint — gardé par `tests/test_odds500.py::TestRobotsTxt`.
- Couche IA (mission 4, 2026-08-22) : `core/ai_router.py` = registre +
  lanes (FILTER/ANALYZE/TRANSLATE_CJK/SEARCH_READ/SETTLEMENT/WIZ) + disjoncteur
  (3 échecs → 30 min) + découverte des catalogues au démarrage du run.
  NE JAMAIS coder un nom de modèle en dur hors du registre : le paysage gratuit
  churne chaque mois. SEUL mort prouvé : GitHub Models (410, corps nommant le
  retrait) ; aussi mort : `meta-llama/llama-3.3-70b-instruct:free` (retiré du
  catalogue :free OpenRouter — le repli était mort en silence).
  ⚠️ Cerebras avait été retiré À TORT sur un 403 SANS CLÉ : un 401/403 sans clé
  ne prouve JAMAIS qu'un palier a fermé, il faut une clé INVALIDE pour trancher
  (Cerebras rend alors 401 wrong_api_key). Rétabli au registre.
  `ai_search.py` délègue au routeur ; Mistral reste HORS registre (Wiz).
  Réserve settlement gardée EN NÉGATIF (les autres lanes sont amputées, elles
  n'y accèdent jamais) — leçon du 2026-08-02. Un compte par fournisseur ;
  `terms_flag` (non_commercial/evaluation) = exclu de la production par défaut.
  Zéro fournisseur configuré n'alerte PAS (sinon spam Telegram en mode REPRICE).
  RÉPARTITION 24h : `lane_providers(balanced=True)` trie par budget RESTANT, pas
  par ordre du registre — sinon le 1er fournisseur est drainé pendant que les
  autres restent intacts (mesuré : 240 appels tous sur Groq → 42 après). Ordre du
  registre = départage à égalité seulement. `ai_complete` interroge le ROUTEUR
  AVANT Groq (Groq = seul à porter compound-mini/recherche web, quota irremplaçable) ;
  `ai_search_complete` garde l'ordre inverse. Budget Groq = 160 req/j, dérivé de
  son TPD 100k et non d'un nombre de requêtes inventé.
  Un catalogue lisible ne prouve RIEN : Cerebras/SambaNova/Chutes rendent 200 sur
  /models et 402 à l'inférence ; Scaleway rend 429 quota-zéro. `ops.py ai` fait le
  vrai appel — c'est le seul diagnostic qui tranche.
- LISTES QUI DIVERGENT = la panne la plus fréquente de ce dépôt (3 occurrences
  le 2026-08-22, toutes silencieuses). Un fournisseur IA sans clé est ignoré
  SANS ERREUR — propriété désirable, mais elle laisse une capacité morte des
  mois. Ne JAMAIS tenir à la main une liste qui existe déjà ailleurs : soit on
  la dérive (`ops.py::_AI_SECRETS` ← `REGISTRY`, tables sport injectées dans
  les templates), soit un test la compare à sa source. Gardiens :
  `tests/test_workflow_secrets.py` (clés IA × workflows × `ops.py` ×
  `.env.example`, bornes de durée, version de Python unique) et
  `tests/test_dashboard_sports.py` (sport → emoji/libellé/ordre).
  AVANT d'ajouter un fournisseur, un sport ou un workflow : lire AUDIT.md §2.
- Le dashboard écrit DEUX fois : `/api/scan` (demande de scan dans `meta`,
  cooldown 120 s) et `/api/audit/run` (déclenche `audit.yml`). Cette dernière
  exige `DASHBOARD_ADMIN_TOKEN` et ÉCHOUE FERMÉ depuis le 2026-08-22 — elle
  était ouverte à tout Internet. Ne pas « réparer » son 401 en retirant la
  garde.
- Une version, un seul endroit : `DASHBOARD_VERSION` (`api/index.py`), injectée
  dans les 6 templates et rendue par `/api/health`. Ne jamais réécrire un
  numéro de version dans un pied de page.
- Wiz — sources : `gather()` FUSIONNE Google News et Bing (jamais « la première
  qui répond » : Google répond toujours, donc Bing ne serait jamais interrogé),
  puis trie LES FAITS D'ABORD avant de tronquer à `MAX_TOTAL`. Google News ne
  rend que des titres nus (sa `<description>` RSS recopie le titre) ; sans ce
  tri, le plafond gardait 58 % de titres sans fait et jetait les extraits de
  Bing — mesuré 5/12 items porteurs de faits, 10/12 après. Un modèle à qui l'on
  sert des titres répond INDISPONIBLE, et il a raison. Gardé par
  `tests/test_wiz_sources.py::TestLesFaitsDabord`.
- DEUX interpréteurs, subis et non choisis : `.python-version` + `vercel.json`
  = **3.12** (l'image de build Vercel n'embarque PAS 3.11 — l'y « aligner »
  casse le déploiement et laisse la prod sur le commit précédent, vécu le
  2026-08-22) ; les 14 workflows et le dev local = **3.11**. Gardé par
  `tests/test_workflow_secrets.py`.
- Une suite verte ne prouve RIEN sur le déploiement : aucun test ne déploie.
  Après toute retouche de `vercel.json`, `.python-version`, `requirements.txt`
  ou `api/index.py` : `python scripts/ops.py vercel deployments | head -3`
  (READY, pas ERROR) puis `curl .../api/health`.
- ÉPOQUE ZÉRO = août 2026. `core/perf_view.PERF_START_MONTH` masque tout mois
  antérieur sur /performance, et juillet 2026 a été ARCHIVÉ en base
  (`sql/migrate_v10_5_archive_pre_august.sql` : 206 lignes vers
  `ai_learning_ledger_archive`, 7 signaux vers `signals_archive`). Décision
  opérateur : « predator n'était pas au point en juillet ». Ne pas
  « réparer » l'absence de juillet ; pour le rouvrir il faut restaurer
  l'archive ET abaisser PERF_START_MONTH — les deux, sinon rien ne s'affiche.
  Archiver, JAMAIS supprimer sèchement : ces lignes sont la seule trace
  empirique et tout backtest qui les ignorerait aurait un biais de survie.
- /performance est volontairement DÉPOUILLÉE (2026-08-22) : plus de seuils
  appris, ni de cycle d'apprentissage, ni de calibration Brier, ni de
  découpage mensuel — rouages internes, pas résultats. Mais la règle « jamais
  un taux de réussite nu » TIENT TOUJOURS : Wilson + seuil rentable après
  taxe sont désormais rendus en une phrase française (« il faut 57 % pour
  être rentable, et 85 paris ne suffisent pas à le prouver »). Ne pas
  supprimer cette ligne en croyant simplifier : c'est une garde de sûreté.
- Wiz — REQUÊTES : jamais de phrase en ET (5 mots ⇒ 0 résultat Google News),
  jamais le vocabulaire des pages de preview (« team news lineup » EST leur
  titre SEO — la requête ramenait 100 % de bruit et Wiz sortait INDISPONIBLE
  partout), jamais de date en terme de recherche (`when:Nd` le fait déjà,
  côté moteur). Forme : `{match} (motA OR motB)`. Le vocabulaire ET la locale
  du flux suivent la langue de la presse locale, déduite du pays préfixant
  `signals.league` (`wiz_engine.press_lang`) — les deux ensemble ou rien :
  des mots espagnols dans l'index `en-US` rendent ZÉRO source. Mesuré :
  3 % → 23 % de sources porteuses de faits. Gardiens :
  `tests/test_wiz_engine.py::TestLangueDeLaPresse`,
  `tests/test_wiz_sources.py::TestLocaleDuFlux`.
- Sub-agent `predator-diagnostician` pour tout audit pipeline/santé (isole les
  gros logs hors de la conversation principale).
