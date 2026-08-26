# PREDATOR PAIM

Pipeline de paris sportifs : ingestion de cotes → moteur de signaux (edge/devig)
→ Supabase → settlement/CLV → couche d'apprentissage → dashboard Flask (Vercel).
Tout le calcul tourne en crons GitHub Actions ; le dashboard est en lecture seule.

## Commandes

- Tests : `python -m pytest tests/ -q` (~45 s, 941 tests, doit rester à 0 échec)
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
- WIZ A ÉTÉ SUPPRIMÉ le 2026-08-26 (page, moteur, workflow, tests, lane du
  routeur) — décision opérateur : « la page wiz ne me sert pas ». Ne pas le
  réintroduire par inadvertance en recréant `/wiz` ou `core/wiz_*`. La table
  `wiz_analysis` a été SUPPRIMÉE de la base le 2026-08-26 (DROP, 748 lignes,
  décision opérateur explicite après présentation de l'option d'archivage —
  `sql/migrate_v10_6_drop_wiz.sql`, appliquée). Il n'existe AUCUNE archive :
  ne pas chercher `wiz_analysis_archive`, elle n'a jamais existé.
  Conséquence directe : MISTRAL EST ENTRÉ AU REGISTRE IA. Il en était exclu
  parce qu'il était le fournisseur unique de Wiz (domaine de panne isolé) ;
  son quota sert désormais la RECHERCHE DE SIGNAUX, lanes `filter`/`analyze`
  seulement. PAS `settlement` (2 req/min, la réserve doit répondre vite) et
  PAS `search_read` (son connecteur `web_search` avait son quota épuisé au
  niveau du COMPTE — l'y enrôler promettrait une capacité inexistante).
  ⚠️ Ses modèles et ses budgets sont les SEULS du registre à n'avoir jamais
  été validés par une inférence réelle : la clé n'est pas disponible en dev.
  Premier geste après déploiement : `python scripts/ops.py ai`.
  Gardien : `tests/test_ai_router.py::TestLanes`.
- Secrets : `core/secret_store.py` (table Supabase `app_secrets`) bat `os.environ` ;
  une valeur périmée dans la table gagne quand même.
- Sources de cotes : la règle est « authentifié par clé = joignable, sinon filtré
  par IP depuis les runners » (LineFeed/ESPN/SofaScore sont morts ; 1xbet rend
  203, 22bet 404 — le harvest soft direct ne ramène plus rien). `ops.py sources`
  les sonde. Depuis l'obsolescence d'OddsAPI, les seules sources qui portent
  RÉELLEMENT des signaux sont api-sports (foot : ~36 matchs, 100 % avec sharp
  Pinnacle) et titan007 (~21-35 matchs, ~19-31 sharp) ; odds-api.io fournit du
  SOFT PUR (100-150 matchs, ZÉRO sharp — de la donnée, pas de l'edge) ;
  Matchbook fournit du SHARP PUR gratuit et illimité (141-202 marchés) et
  produit pourtant 0 signal. ⚠️ CE N'EST NI UN PROBLÈME DE COUVERTURE NI DE
  NOMS — l'hypothèse « seuls ~6 % s'apparient » était FAUSSE, vérifiée en
  direct le 2026-08-26 : Matchbook cote bien les ligues du slate (7 matchs
  d'OBOS-ligaen, 3 d'Argentine B ce jour-là), et les 4 signaux du run de
  09:47 (Moss, Stabæk, Acassuso, Liniers) avaient TOUS leur marché
  Matchbook ; `strict_team_match` les apparie correctement, diacritiques
  norvégiennes comprises. La vraie cause est `_enrich_from_exchange`
  (`run_engine.py`) : `if pin["1"] > 1.01 and pin["2"] > 1.01: continue` —
  Matchbook n'est consulté que sur les matchs SANS prix sharp. api-sports
  livrant Pinnacle sur 100 % de ses matchs foot, il est écarté précisément
  sur ceux qui portent les signaux. Il est câblé en BOUCHE-TROU, pas en
  CONTRE-EXPERTISE. Le gisement est là : un 2e avis sharp indépendant
  détecterait le Pinnacle PÉRIMÉ, qui est la fabrique à faux edge — et sa
  couverture du slate est sans commune mesure avec celle de
  Kalshi/Polymarket (3 fixtures exploitables sur 70). Mesuré sur
  10 runs du 2026-08-23 au 26. Corollaire : 100 % des signaux sont du FOOTBALL.
- ODDSAPI EST OBSOLÈTE (décision opérateur 2026-08-26) : `ODDS_API_ENABLED`
  (`run_engine.py`) vaut 0 par défaut, le Tier 1 ne s'exécute plus, aucune
  alerte de pool ne part (un pool mort est l'état NOMINAL, pas une panne).
  Réactivation : `ODDS_API=1`. Le module `core/odds_api.py` RESTE — il n'est
  pas qu'une source, ses `SPORT_KEYS` sont le vocabulaire écrit dans
  `signals.sport` et relu par `api/index.py` (invariant des sport-keys,
  AUDIT.md §2). Ce que l'obsolescence tue, en clair : tennis, hockey,
  MMA/boxe, NFL/NCAAF, LdC/UEL, Euroleague (aucune source gratuite ne les
  price), et la capture closing-line « en stop » sur le payload payant —
  seul `run_closing_line.py` la fait encore, donc le CLV réel se raréfie
  alors que `learning_layer` en fait un critère de premier rang. La garde
  `[ -z "$ODDS_API_KEY" ] && exit 1` a été retirée d'engine/golden_hour/
  deep_scan : elle échouait FERMÉ et aurait tué tous les scans le jour où le
  secret est retiré. La sortie anticipée de GOLDEN_HOUR supposait un Tier 1
  vivant : sans elle, golden_hour.yml était un no-op horaire permanent.
  Gardien : `tests/test_oddsapi_obsolete.py`.
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
  Curseur `meta.sevenm_sitemap_cursor` OBLIGATOIRE : le sitemap 7M (435 ids
  au 2026-08-26, pas 936) commence par des coupes mineures sans recoupement —
  sans curseur, 0 alias appris à chaque run et branchement inerte en silence.
  Le curseur NE SUFFIT PAS : mesuré le 2026-08-26 sur 30 ids de tête, 0 échec
  de requête mais **27 matchs DÉJÀ JOUÉS** et 3 utiles seulement (le sitemap
  n'est pas trié par coup d'envoi et traîne plusieurs jours de passé). D'où
  `meta.sevenm_past_gids` : un match joué ne redevient jamais à venir, on ne
  le repaie donc plus jamais. Rendement mesuré 10 % → 30 % dès le 2e run, et
  croissant. La mémoire est refermée sur le sitemap courant à chaque écriture,
  sinon elle gonfle sans fin.
- ODDS500 N'EST PAS EN PANNE, ELLE EST FILTRÉE PAR IP (2026-08-26) : HTTP 200
  et 15 fixtures depuis un poste de dev, `Connection refused` depuis les
  runners GitHub. Le code, le parseur et le User-Agent vont bien — aucune
  correction de code ne lève un blocage d'IP. Seule issue : `core/net.py`
  (`FREE_SOURCES_PROXY`, ou `ODDS500_PROXY`/`SEVENM_PROXY` par source). Sans
  variable, le module est INERTE et rien ne change. Plomberie COMPLÈTE au
  2026-08-26 : lecture par `secret_store` (donc `app_secrets` AVANT l'env —
  URL rotative sans redéploiement), les 3 variables sont transmises par
  engine/golden_hour/deep_scan/guerrilla, documentées dans `.env.example`,
  et `ops.py sources` affiche `[via proxy]`. ⚠️ `proxy_for` MÉMORISE sa
  résolution pour tout le processus : `get_secret` ne met pas les valeurs
  ABSENTES en cache, et l'absence de proxy étant le cas nominal, chaque
  requête HTTP d'odds500 aurait relu Supabase. `net.reset()` pour les tests.
  Chemin proxy VÉRIFIÉ de bout en bout : proxy vivant → 99 420 caractères
  et 15 fixtures ; proxy mort → échec (donc rien ne le contourne) ; sans
  proxy → succès direct. ⚠️ Un test depuis un poste de dev ne prouve RIEN
  sur les runners, où ça marche déjà sans proxy — seul un run GitHub tranche.
  VOIE RETENUE (décision opérateur 2026-08-26) : RELAIS Cloudflare Worker,
  `scripts/cloudflare_relay_worker.js`. Un Worker ne parle pas CONNECT — ce
  sont donc DEUX mécanismes distincts dans `net.py`, pas deux réglages du
  même : `prepare()` réécrit l'URL (relais), `opener_for()` tunnelise
  (proxy). Le relais gagne si les deux sont posés. Variables :
  `FREE_SOURCES_RELAY` + `FREE_SOURCES_RELAY_TOKEN` (et `ODDS500_RELAY` /
  `SEVENM_RELAY`), câblées dans les 4 workflows de scan.
  DEUX GARDES NON NÉGOCIABLES côté Worker, sans quoi c'est un PROXY OUVERT
  que le premier venu utilisera sur ton quota : jeton partagé (comparé en
  temps constant) ET liste blanche d'hôtes. Gardées par
  `tests/test_free_sources_wiring.py::TestModeRelais`.
  Le Worker doit rendre `upstream.body` (octets bruts) et JAMAIS `.text()` :
  500.com sert du GB18030, un passage par le texte rendrait tous les noms
  chinois en mojibake — panne silencieuse ressemblant à un parseur cassé.
  Chemin relais VÉRIFIÉ de bout en bout contre un serveur local conforme :
  15 fixtures, `大田市民 vs 蔚山现代` intact ; jeton faux → 403 ; hôte hors
  liste avec jeton valide → 403 ; 7M → 435 ids.
  EN PRODUCTION DEPUIS LE 2026-08-26 : Worker `predator-relay` déployé sur le
  compte, sous-domaine `predator-relay.ipotradingbot.workers.dev`, et les deux
  secrets GitHub posés. ⚠️ LE PIÈGE QUI A COÛTÉ LE PLUS DE TEMPS : le Worker
  était uploadé ET son binding `RELAY_TOKEN` présent, mais le sous-domaine
  `workers.dev` était DÉSACTIVÉ — le script n'avait donc aucune URL publique
  et rendait 404 sur tout. Un `workers/scripts` qui liste le Worker ne prouve
  PAS qu'il est joignable : vérifier `GET workers/scripts/<nom>/subdomain`
  (`enabled: true`). La valeur d'un `RELAY_TOKEN` déjà posé étant ILLISIBLE,
  la seule façon de faire correspondre les deux côtés est de le faire tourner.
  ✅ LEVÉ — que 500.com accepte les IP de sortie de Cloudflare : mesuré le
  2026-08-26, 200 et 58 807 octets à travers le relais, soit exactement la
  taille obtenue en direct. Pas de 502, donc pas de proxy à IP dédiée à
  chercher. Encodage vérifié à travers le relais : 518 noms chinois, ZÉRO
  mojibake. 7M a été joint pour la PREMIÈRE fois (435 ids) — sa joignabilité
  n'est plus inconnue. `ops.py sources` affiche `[via relais Cloudflare]` sur
  les deux. Reste le seul juge qui compte : un run GitHub Actions (un poste de
  dev joint déjà 500.com en direct, il ne prouve rien).
- Kalshi/Polymarket : BRANCHÉS le 2026-08-26 (`free_sources.measure_slate_consensus`,
  appelé par `harvester._fetch_multi_book`). Ils étaient importés NULLE PART
  hors de leurs tests depuis le 2026-08-22 — capacité morte en silence. Rôle
  `consensus` : ils MESURENT et n'émettent jamais, ne repricent rien ; ils
  crient quand un prix du slate diverge d'un marché qui ne recopie aucun
  bookmaker (un « edge » qui est en fait un prix périmé). Couverture honnête :
  EPL/UCL/NFL/NBA seulement, et sur 70 fixtures EPL vivantes, **3** portent
  des cotes exploitables. Le recoupement avec un slate fait de ligues mineures
  est donc structurellement faible — c'est un garde-fou, pas un gisement.
- Pièges qui tuent une source en silence : User-Agent avec un accent → urllib encode
  en latin-1 → 403 Cloudflare (Polymarket) ; Kalshi rend `yes_bid`/`volume` à `null`
  et met les prix dans les champs `*_dollars` en chaîne ; un 1X2 amputé d'une patte
  devient indiscernable d'un moneyline et s'apparie avec lui ; la `<description>`
  d'un item Google News RSS est le TITRE recopié, pas un extrait — une source qui
  « répond » peut ne porter aucun fait (mesuré sur /wiz avant sa suppression,
  2026-08-22 — la leçon vaut pour toute source d'actualité).
- robots.txt : sur odds.500.com la QUERY STRING est la frontière (`/fenxi/ouzhi-*.shtml`
  autorisé nu, interdit avec `?ctype=`/`?order=`/`?cids=`). Ne jamais paramétrer un
  endpoint — gardé par `tests/test_odds500.py::TestRobotsTxt`.
- Couche IA (mission 4, 2026-08-22) : `core/ai_router.py` = registre +
  lanes (FILTER/ANALYZE/TRANSLATE_CJK/SEARCH_READ/SETTLEMENT) + disjoncteur
  (3 échecs → 30 min) + découverte des catalogues au démarrage du run.
  NE JAMAIS coder un nom de modèle en dur hors du registre : le paysage gratuit
  churne chaque mois. SEUL mort prouvé : GitHub Models (410, corps nommant le
  retrait) ; aussi mort : `meta-llama/llama-3.3-70b-instruct:free` (retiré du
  catalogue :free OpenRouter — le repli était mort en silence) ; et depuis le
  2026-08-26 `llama-3.3-70b-versatile` + `llama-3.1-8b-instant`, DISPARUS du
  catalogue Groq (14 modèles, plus aucun llama de génération). Groq tourne
  désormais sur `qwen/qwen3.8-27b` — le SEUL instruct du nouveau catalogue :
  les `openai/gpt-oss-*` sont des modèles de RAISONNEMENT qui rendent un
  contenu VIDE sous les plafonds serrés du pipeline (max_tokens=80 pour un
  alias), ils restent en repli pour les appels à 2048. Corollaire : le palier
  `tier` de `ai_complete` ne réordonne PLUS les modèles (il ne choisit que la
  lane du repli) — inverser remettrait un modèle de raisonnement en tête.
  ⚠️ Cette panne-là a coûté cher parce que `ai_search.py` portait TROIS copies
  à la main des modèles Groq et appelait Groq EN DIRECT : le routeur écartait
  Groq proprement pendant que le vrai chemin d'appel tapait un modèle mort en
  404, avec backoff, jusqu'au timeout global de 540 s qui tuait le Deep Scan
  du matin. Les listes sont maintenant DÉRIVÉES (`ai_search._groq_models`) et
  gardées par `tests/test_ai_router.py::TestAucunModeleEnDurHorsDuRegistre`,
  qui refuse tout littéral de modèle du registre ailleurs que dans
  `ai_router.py` (vérifié sur l'AST : un commentaire a le droit de nommer un
  modèle mort pour raconter pourquoi il l'est).
  ⚠️ Cerebras avait été retiré À TORT sur un 403 SANS CLÉ : un 401/403 sans clé
  ne prouve JAMAIS qu'un palier a fermé, il faut une clé INVALIDE pour trancher
  (Cerebras rend alors 401 wrong_api_key). Rétabli au registre.
  `ai_search.py` délègue au routeur ; Mistral y est ENTRÉ le 2026-08-26.
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
- Sub-agent `predator-diagnostician` pour tout audit pipeline/santé (isole les
  gros logs hors de la conversation principale).
