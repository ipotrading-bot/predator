# Plan de refonte CI — dépôt `predator`

> Phase 0 (planification). Rédigé le 2026-08-26 sur la branche `refactor/ci-v2`.
> **Aucun fichier du dépôt n'a été modifié** en dehors de celui-ci.
> Baseline mesurée avant tout changement : `945 passed` en 38,5 s, pyflakes propre.

---

## 1. Vérification des constats F1–F7

### F1 — 13 workflows, 1 561 lignes, 339 références `secrets.` — **CONFIRMÉ**

```
1561 total  (audit 191 · backfill 65 · closing_line 113 · deep_scan 210 · deploy 31
             engine 224 · golden_hour 299 · guerrilla 208 · monte_carlo 45
             probe_xbet 36 · rank_sports 59 · rapport 48 · tests 32)
339 occurrences de `secrets.`
```

Les quatre scans lancent tous `python run_engine.py`
(`engine.yml`, `golden_hour.yml`, `deep_scan.yml`, `guerrilla.yml`).
Le bloc « Verify required secrets » est présent dans **8** fichiers
(backfill, audit, closing_line, golden_hour, deep_scan, monte_carlo, engine, guerrilla).

**Nuance ajoutée (elle renforce le constat).** Le registre IA compte **18**
fournisseurs ; les workflows n'en câblent que **15**. `CEREBRAS_API_KEY`,
`CHUTES_API_KEY`, `SAMBANOVA_API_KEY` et `ZHIPU_API_KEY` ne sont dans **aucun**
workflow. Ce sont aujourd'hui des `terms_flag`/non-PRODUCTION_SAFE, donc sans
effet en production — mais c'est très exactement la divergence que la refonte
supprime, et elle est déjà présente, en silence, au moment où j'écris.

### F2 — 196 déclenchements/jour dont 144 pour `closing_line.yml` — **CONFIRMÉ, avec une nuance qui compte**

`core/constants.py` l. 104-107 : `CLOSING_LINE_WINDOW_MIN = 240`,
`TIGHTEN_MIN = 90`, `REFRESH_MIN = 20`. `_needs_refresh`
(`core/audit_engine.py:280`) refuse de re-pricer un signal déjà tarifé hors des
90 min, et jamais plus souvent que 20 min. L'en-tête de `closing_line.yml`
parle bien d'une « 5-minute pre-kickoff window » : **périmé**.

Décompte planifié : engine 8 + golden 24 + deep 2 + guerrilla 2 + closing 144
+ audit 4 + rapport 12 + rank_sports 0,14 = **196,1/jour**. ✔

**La nuance.** Le commentaire du cron actuel n'est pas naïf : il justifie
`*/10` non par la fenêtre mais par le **taux de livraison** de GitHub —
0,48 exécution/h mesurée pour un cron horaire. Demander 6 ticks/h en fait
atterrir ~2,9, soit un espacement réel proche des 20 min de `REFRESH_MIN`.
Passer à 3 ticks/h demandés donnera ~1,4 tick/h livré, soit un espacement
médian de ~40 min : **on perd bien du travail utile**, ce n'est pas
purement du no-op.

Ce que la refonte y oppose : les 36 passes post-scan. 72 + 36 = 108 passes/j,
mais elles ne sont **pas** distribuées comme les 144 ticks — elles sont
agglutinées sur les minutes de scan. Un match dont le coup d'envoi tombe dans
un creux de scan sera vu moins souvent qu'aujourd'hui.

> **Décision proposée :** appliquer `14,34,54` comme demandé, et surveiller
> `count_missed_closing_lines` (déjà loggé par `run_closing_line.py`) sur une
> semaine. Si le compte de ratés monte, le retour arrière est d'une ligne.
> Je ne modifie aucun cron de ma propre initiative (consigne §7).

### F3 — `closing_line.yml` dans le verrou ne protège rien — **NUANCÉ : la conclusion tient, le raisonnement est faux**

Vérifié :
- `fetch_closing_line_candidates` (`core/audit_engine.py:246`) → `status='active'`
  **et** `match_time >= now` (l. 270). Le commentaire l. 261 dit que
  `match_time >= now` est *load-bearing*.
- `fetch_pending` (`core/audit_engine.py:76`) → `status='active'` et
  `match_time < now − 4 h`. **Aucune intersection avec la closing line** : ✔
  le settlement et la capture ne peuvent pas toucher la même ligne.
- **Mais la purge, si.** `_purge_old_signals` (`run_engine.py:875`) ne se limite
  pas aux matchs passés. Ses `purge_rules` (l. 928-937) suppriment des lignes
  `status='active'` **sans aucun filtre sur `match_time`** :
  `edge_pct > 15`, `edge_pct <= floor`, `sharp_prob <= 0`, `market IS NULL`,
  `risk_flag = 'SUSPECT_DATA'`, `xbet_odd <= 1.01`, `pinnacle_price <= 1.01`,
  plus la purge h2h SUSPECT (l. 969) et les `market_key` légataires (l. 981).
  Une ligne active à coup d'envoi futur peut donc être supprimée par un scan
  pendant que la capture l'écrit.

**Pourquoi la conclusion tient quand même.** Postgres sérialise DELETE et
UPDATE ligne par ligne : la collision ne corrompt rien, elle fait au pire
perdre une écriture sur une ligne que la purge condamnait de toute façon
(elle sort du dépôt à l'instant d'après). Le verrou n'achète donc pas de
correction — il achète de l'attente derrière un deep scan de 25 min.

> Le commentaire à écrire dans `closing_line.yml` ne doit **pas** dire
> « aucune ligne en commun » : c'est faux et la prochaine personne qui lira
> `purge_rules` perdra une heure à s'en convaincre. Formulation que je
> propose : « le settlement ne peut pas toucher ces lignes (`match_time` de
> part et d'autre de `now`) ; la purge le peut, mais seulement pour les
> supprimer, et Postgres sérialise déjà cette course ligne par ligne. »

### F4 — les commentaires mentent sur `_save` — **CONFIRMÉ**

`run_engine.py:364-381` : `_save` est bien un `select` → `update` en place /
`insert`, et sa docstring explique que c'est précisément pour ne plus effacer
les `CLOSING_LINE_COLS`. Le verrou scan ↔ audit reste justifié par
`_purge_old_signals`, pas par `_save`. ✔

### F5 — la capture gratuite est morte avec OddsAPI — **CONFIRMÉ**

`capture_from_scan` n'est appelée qu'en `run_engine.py:1980`, à l'intérieur de
`if oddsapi_events:` (l. 1963), lui-même sous
`if ODDS_API_ENABLED and not GUERRILLA and not REPRICE:` (l. 1951).
`ODDS_API_ENABLED = os.environ.get("ODDS_API", "0") == "1"` (l. 76) → **la
branche ne s'exécute jamais**. Seul reste l'oracle web-search
(`capture_closing_lines`, h2h uniquement, budget `CLOSING_LINE_BUDGET = 30`).

Et `fetch_matchbook_prices` est bien appelé à chaque scan, REPRICE compris
(`run_engine.py:2025`), sans que ces prix servent jamais à la clôture. ✔

### F6 — trois points — **CONFIRMÉS tous les trois**

- `api/index.py:794` : `…/actions/workflows/audit.yml/dispatches` — le nom du
  fichier est une dépendance de production. **`audit.yml` est intouchable.**
- `scripts/ops.py:248` `_ai_secrets()` dérive déjà de `core.ai_router.REGISTRY`.
- `core/ai_router.py:84` `import requests` → `scripts/ci_env.py` **n'est pas
  importable avant `pip install -r requirements.txt`**.

> Conséquence sur `.github/actions/setup/action.yml` : le préflight doit rester
> le **dernier** step de l'action, après l'installation. C'est bien le cas dans
> le fichier fourni. À ne jamais réordonner « pour échouer plus tôt » — on
> échouerait sur un `ModuleNotFoundError` au lieu du message utile.

### F7 — double déploiement Vercel — **CONFIRMÉ, et mesuré**

`vercel.json` ne contient que `version` et `PYTHON_VERSION: 3.12` : le
déploiement Git n'est pas désactivé. L'API Vercel donne la source de chaque
déploiement :

```
git  READY 482ab2a docs(relais): 403 tranché…          ← Git seul (commit hors paths de deploy.yml)
cli  READY 249ac32 fix(groq): cooldown…                ┐ MÊME COMMIT
git  READY 249ac32 fix(groq): cooldown…                ┘ DÉPLOYÉ DEUX FOIS
cli  READY 3d639b4 feat(ui): nav mobile…               ┐
git  READY 3d639b4 feat(ui): nav mobile…               ┘
cli  READY d94ea7c … / git READY d94ea7c …             (idem)
cli  READY 9aef90d … / git READY 9aef90d …             (idem)
```

Tout commit touchant `api/**`, `templates/**`, `core/**` est déployé **deux
fois**, en course. `tests.yml` et `deploy.yml` sont bien deux workflows
indépendants déclenchés en parallèle sur `push`.

**Conséquence non écrite dans le diagnostic, et elle est déterminante :** tant
que le déploiement Git reste actif, gater le déploiement CLI derrière les tests
**ne protège rien** — le déploiement Git partira quand même, sans attendre.
Le chantier C n'est pas une finition du chantier A : c'est ce qui rend le
`needs: test` réel.

---

## 2. Écarts entre les fichiers fournis et l'état de `main`

| # | Fourni | `main` | Traitement proposé |
|---|--------|--------|--------------------|
| E1 | `cron: '3 2,6,9,12,17,19,21,23 * * *'` | **8 lignes séparées** `'3 2 * * *'` … `'3 23 * * *'` | **Prendre la forme fournie.** `github.event.schedule` rend la chaîne exacte : sans fusion, il faudrait 8 lignes dans `CRON_MODES`. Cadence identique. |
| E2 | `cron: '33 5,17 * * *'` | 2 lignes séparées | idem E1. |
| E3 | `MODE_ENV['guerrilla']` sans `HOURS_AHEAD` | `guerrilla.yml` n'en pose pas non plus | ✔ conforme — le 48 h vient du **code** (`run_engine.py:1903`), pas du workflow. Le commentaire « horizon 48 h » est donc juste. |
| E4 | `MODE_ENV['deep'] = {DEEP_SCAN:1, HOURS_AHEAD:24}` | identique | ✔ `MAX_MATCHES=100` vient du code (`run_engine.py:188`), rien à poser. |
| E5 | `MODE_ENV['guerrilla']` : `PINNACLE_BATCH: 25`, `MAX_ORACLE: 3` | identiques **aux défauts du code** | ✔ conforme à `main`. Ce sont des redites inoffensives ; je les conserve à l'identique plutôt que d'introduire un écart. |
| E6 | `CACHE_ESPORTS_TTL_H: 4` | présent dans `guerrilla.yml` | ✔ conforme, **mais eSports a été RETIRÉ le 2026-08-22** (`RETIRED_SPORTS`). Variable morte des deux côtés. Je la reporte telle quelle (consigne : reporter `main`) et je la signale ici plutôt que de la supprimer en douce. |
| E7 | `reports.yml` `push.paths` = 3 scripts | `rank_sports.yml` a `rank_sports.py`, `calibration_report.py`, **et le workflow lui-même** ; pas `weekly_report.py` | Prendre la version fournie — elle corrige un vrai trou (toucher `weekly_report.py` ne rejouait rien). |
| E8 | `timeout-minutes: 30` pour le scan | 15 / 15 / 25 / 15 | Fourni. Rappel : c'est une borne, pas un coût. |
| E9 | `tests/test_workflow_secrets.py` fourni supprime 4 tests IA et en garde 9 | 13 fonctions, 58 tests collectés avec `test_sport_verdicts` | Attendu : la couverture IA migre vers `test_ci_env.py`. |
| E10 | « 958 passed » | baseline **945** | Je ne garantis pas 958 : le total dépendra du nombre de fichiers dans `.github/workflows/` (tests paramétrés). Critère réel : **0 échec** et aucun test perdu sans remplaçant. |

### Écarts de comportement induits (à valider, ils ne sont pas neutres)

1. **Le bouton « Scanner » change de sémantique.** Aujourd'hui `meta.scan_request`
   n'est lu que par `golden_hour.yml` (24 ticks/j) et promeut ce tick en
   `GUERRILLA=1` **nu** (sans `SEARCH_MAX_TOKENS`, `TAVILY_RUN_BUDGET`, …).
   Après : le flag est lu et **effacé à chacun des 36 ticks**, mais `promote()`
   ne promeut que le mode `golden`. Deux conséquences :
   - un clic consommé par un tick `standard`/`deep`/`guerrilla` est **avalé
     sans promotion** (le scan tourne, mais pas en guerrilla) ;
   - un clic consommé par un tick `golden` déclenche désormais un guerrilla
     **complet**, avec `TAVILY_RUN_BUDGET=40` au lieu de 25 — plus cher qu'aujourd'hui.

   > Ni l'un ni l'autre n'est cassé, mais ce n'est pas ce que fait `main`.
   > **Question ouverte n° 1** ci-dessous.

2. **Un push purement documentaire ne redéploie plus.** Aujourd'hui le Git
   auto-deploy déploie *tout* push sur main (cf. 482ab2a). Après le chantier C,
   seul `ci.yml` déploie, sur ses `paths`. C'est correct — mais c'est un
   changement.

3. **`push` sur `guerrilla.yml` ne relance plus de scan** (déjà énoncé §7 du brief).

4. **Le pool `scan` transmettra 18 clés IA au lieu de 15** (cf. F1). Extension,
   pas régression.

5. **La file d'attente du verrou.** `concurrency` ne garde **qu'un** run en
   attente par groupe : un troisième arrivant annule celui qui patientait.
   `predator-signals-write` porte aujourd'hui 38 runs/j (34 scans + 4 audits) ;
   après, 40 (36 + 4), avec un plafond de scan porté à 30 min et une passe
   closing line ajoutée dans le verrou. Le risque d'annulation en file
   **augmente**. Il reste faible aux cadences en jeu, mais il est réel et je
   préfère l'écrire avant qu'après.

---

## 3. Chantier B — le point où l'adaptateur peut se tromper

**Question posée :** après `_enrich_from_exchange`, quelle clé du dict `match`
porte le prix sharp de chaque côté h2h, et sous quel `match_id` les signaux
correspondants sont-ils stockés ?

**Réponse — clés.** `_enrich_from_exchange` (`run_engine.py:770`) écrit :

```python
m["odds_pinnacle"] = {"1": bf["1"], "X": bf.get("X", 0.0), "2": bf["2"]}   # l. 795
m["_exchange"] = src            # "matchbook" | "betfair"
m["_betfair"]  = True
m["totals_pinnacle"]  = bf["totals"]    # seulement si absent
m["spreads_pinnacle"] = bf["spreads"]   # seulement si absent
```

Donc : `m["odds_pinnacle"]["1"]` = domicile, `["2"]` = extérieur, `["X"]` = nul,
et `m["_exchange"]` est le seul marqueur de provenance.

**Réponse — `match_id`.** Les signaux sont stockés sous `match_id = m["id"]`
(`_emit(..., match_id=m.get("id",""))`, l. 1282 h2h, 1379 totals, 1443 spreads),
c'est-à-dire l'identifiant **de la source soft** (`harvester._stable_id`, `CI`
1xbet, `o500_*`). `core/closing_line._fetch_signals` filtre déjà sur
`in_("match_id", chunk)` : la mécanique de `capture_from_scan` se transpose
telle quelle.

### ⚠️ Le piège, et c'est celui que le brief anticipait

`_enrich_from_exchange` s'ouvre sur (l. 789) :

```python
if pin.get("1", 0) > 1.01 and pin.get("2", 0) > 1.01 and not m.get("_estimated"):
    continue
```

**Il n'écrit `odds_pinnacle` que sur les matchs SANS prix sharp.** Or api-sports
livre Pinnacle sur 100 % de ses matchs foot, et 100 % des signaux du dépôt sont
du football (CLAUDE.md). Sur les matchs qui portent les signaux,
`odds_pinnacle` reste donc le **Pinnacle d'api-sports** et `_exchange` **n'est
jamais posé**.

> **Un `capture_from_exchange` qui lirait `m["odds_pinnacle"]` après
> enrichissement ne capturerait jamais rien sur les signaux réels — et pire, il
> écrirait un `closing_source='exchange'` portant en réalité le prix Pinnacle
> d'entrée, donc un CLV structurellement nul. Panne silencieuse, verte, et qui
> ressemblerait à un succès.**

**Conception retenue :** la fonction reçoit le **dict de prix d'exchange brut**
et fait sa propre résolution :

```python
capture_from_exchange(sb, matches, exchange_prices, now, window_min=CLOSING_LINE_WINDOW_MIN)
```

avec, par match dans `[now, now+window]`, un `_lookup_exchange(m, exchange_prices)`
(exact → exact inversé → `strict_team_match` unique, `run_engine.py:719`) —
indépendamment de tout enrichissement. `_lookup_exchange` et
`_flip_exchange_prices` devront être importables : soit déplacés dans
`core/matchbook.py`/`core/source_adapter.py`, soit passés en callback. Je
propose l'import depuis `run_engine` pour ne rien déplacer au premier jet
(à trancher à l'implémentation, cf. question ouverte n° 4).

### Second piège : le DNB du football

`_h2h_close` (`core/closing_line.py:141`) applique `calc_dnb(ours, theirs, X)`
pour `sport == "soccer"` — le contrat du module est que le prix de clôture soit
calculé **avec la même maths que le prix d'entrée**. Matchbook fournit bien un X
(`core/matchbook.py:167`, marché `one_x_two`), mais `_flip_exchange_prices`
comme `_enrich_from_exchange` tolèrent `X = 0.0`. Avec `X = 0`, `calc_dnb`
retourne `0.0` (`core/math_engine.py:171-177`).

> Règle à implémenter : **X manquant → on n'écrit rien** et on logge un
> `CLOSE SKIP`. Jamais de repli sur le moneyline brut : ce serait comparer un
> prix DNB d'entrée à un prix ML de clôture, soit un CLV faux et silencieux.

### Autre point vérifié

`signals.closing_source` : à confirmer côté base avant d'écrire du code. Si la
colonne est un `text` libre, aucune migration ; sinon
`sql/migrate_v10_7_closing_source_exchange.sql`, **écrite mais pas appliquée**
(règle du dépôt : le SQL Editor, à la main, par l'opérateur).

---

## 4. Plan d'exécution

### Chantier A — CI (`refactor/ci-v2`, 1 commit)

**Fichiers créés** : `scripts/ci_env.py`, `scripts/ci_scan_mode.py`,
`.github/actions/setup/action.yml`, `.github/workflows/{scan,closing_line,audit,reports,tools,ci}.yml`,
`tests/test_ci_env.py`.
**Réécrit** : `tests/test_workflow_secrets.py`.
**Patché** : `tests/test_sport_verdicts.py:97-103` (`rank_sports.yml` → job `hebdo` de `reports.yml`).
**Supprimés** : les 11 workflows listés au §3.3 du brief + `scripts/probe_xbet_sports.py`
(vérifié : plus aucune référence hors de son propre en-tête et de son workflow).

Ordre : scripts → tests → action → workflows → suppressions → vérifications §3.4.

**Acceptation** : `pytest -q` sans échec ; pyflakes propre ; les 7 YAML parsés ;
les 5 vérifications manuelles du §3.4 rendent exactement les valeurs attendues
(`g3 False False`, `role='anon'` + exit 1, `mode=golden`, cron inconnu + exit 1).

**Risque principal** : un pool incomplet fait tourner un job **sans erreur** avec
une capacité muette — exactement la panne que la refonte combat. Mitigé par
`test_tout_fournisseur_de_production_atteint_les_pools_ia` et par le préflight.
**Risque résiduel non couvert par les tests fournis** : le câblage des relais
(`FREE_SOURCES_RELAY*`, `*_PROXY`) est dans le pool `scan` mais **aucun test ne
le garde** — alors que CLAUDE.md en fait un invariant. Cf. question ouverte n° 3.

**Retour arrière** : `git revert` du commit. Aucun état externe touché (pas de
secret déplacé, pas de migration). Les crons repartent tels quels.

### Chantier B — closing line depuis Matchbook (1 commit)

Étapes 1-5 du brief, avec les deux corrections de conception du §3 ci-dessus
(dict de prix brut plutôt que `m["odds_pinnacle"]` ; refus d'écrire sans X pour
le football). Point d'appel : juste après le bloc `_enrich_from_exchange`
(`run_engine.py:2040`), sous `try/except` en warning, **tous modes y compris
REPRICE**.

**Acceptation** : suite verte ; au premier tick golden après merge, au moins un
`CLOSING LINE | … | exchange` dans les logs pour un h2h à moins de 4 h du coup
d'envoi ; `Closing-line capture done: N` en baisse côté oracle.
**Risque** : écrire un `closing_source='exchange'` qui porte en fait le prix
d'entrée (cf. le piège) → le test « côté résolu → prix du bon côté » doit être
construit avec un `odds_pinnacle` d'entrée **différent** du prix Matchbook,
sinon il passerait aussi avec l'implémentation fausse.
**Retour arrière** : revert ; les lignes déjà écrites restent, `closing_source`
permet de les isoler.

### Chantier C — Vercel (1 commit + gestes manuels de l'opérateur)

1. `vercel.json` ← `"git": {"deploymentEnabled": {"main": false}}` (rien d'autre
   n'est touché : `PYTHON_VERSION: 3.12` est intouchable).
2. Secrets `VERCEL_*` → environnement GitHub. **Je ne peux pas le faire :**
   `gh secret list` rend `HTTP 403: Resource not accessible by integration` — le
   token de ce Codespace est un token d'App sans le scope `secrets`. Les
   commandes vous seront données, à exécuter vous-même.
3. Après merge : `ops.py vercel deployments | head -3` (READY) puis `/api/health`.

**Découverte qui demande votre arbitrage :** le dépôt a **déjà 12
environnements**, dont un nommé **`Production`** (majuscule, créé par
l'intégration Vercel le 2026-05-03) et onze `Production – predator*`. Les noms
d'environnement GitHub sont insensibles à la casse : `environment: production`
dans `ci.yml` s'attacherait à l'environnement Vercel existant. Cf. question
ouverte n° 2.

### Chantier D — pointeurs et docs (1 commit, commentaires seulement)

Cibles confirmées par `grep` (liste complète), **plus trois que le brief ne
mentionne pas** :

- `core/scan_windows.py:20` (« la cadence d'engine.yml »)
- `.claude/skills/predator-pipeline/SKILL.md:231` — décrit le préflight
  `[ -z "$ODDS_API_KEY" ] && exit 1` **qui a déjà été retiré** : périmé avant
  même cette refonte.
- `.env.example:28` — « Le secret GitHub ODDS_API_KEY doit rester NON VIDE
  (préflight des workflows) » : **faux depuis le 2026-08-26**, et le nouveau
  `test_preflight_odds_api_key_nest_plus_requise` le contredit explicitement.

Plus les cibles du brief : `run_engine.py` (4 mentions), `run_rapport.py` (2),
`run_monte_carlo.py`, `run_closing_line.py`, `core/audit_engine.py:490`,
`api/index.py:857`, `scripts/{rank_sports,calibration_report,weekly_report}.py`,
`README.md:81,202`, `CLAUDE.md`, `.env.example:50,464`,
`.claude/skills/predator-pipeline/SKILL.md` (tableau des cadences).
`reports/*.md` : **non touchés** (rapports datés).

---

## 5. Questions ouvertes — j'ai besoin de vos réponses avant la Phase 1

1. **Bouton « Scanner ».** Le flag `meta.scan_request` sera lu et **effacé** par
   les 36 ticks, mais `promote()` ne promeut que `golden`. Un clic consommé par
   un tick `standard` est donc avalé sans promotion. Trois options :
   (a) garder tel quel — le clic déclenche au pire un scan standard immédiat ;
   (b) faire promouvoir **tout** mode en `guerrilla` ;
   (c) ne lire le flag que sur les ticks `golden` (comportement actuel).
   **Ma recommandation : (a)**, la plus proche du brief et la moins chère ; le
   clic obtient toujours un scan, seule la « saveur » varie.

2. **Nom de l'environnement GitHub.** `Production` existe déjà (Vercel).
   (a) réutiliser `production` (= le même, insensible à la casse) — simple, mais
   mélange les déploiements Vercel et le gate CI ;
   (b) créer `ci-deploy`, distinct. **Ma recommandation : (a)** pour rester
   fidèle au brief, sauf si vous voulez les journaux séparés.

3. **Faut-il un test qui garde le câblage des relais dans le pool `scan` ?**
   CLAUDE.md en fait un invariant ; `tests/test_ci_env.py` tel que fourni ne le
   vérifie pas. C'est trois lignes. **Ma recommandation : oui**, mais cela ajoute
   un test au fichier que vous avez figé — d'où la question.

4. **Chantier B : où vivent `_lookup_exchange`/`_flip_exchange_prices` ?**
   Import depuis `run_engine` (zéro déplacement, mais `core/` importerait la
   racine) ou déplacement vers `core/source_adapter.py` (plus propre, diff plus
   large). **Ma recommandation : déplacement**, la dépendance `core → run_engine`
   serait la première du dépôt.

5. **`closing_source` accepte-t-il une valeur libre ?** À vérifier en base avant
   d'écrire la migration. Je peux le faire au début du chantier B.

---

## 6. Ce que je n'ai pas fait

- Aucun fichier du dépôt modifié ; branche `refactor/ci-v2` créée, ce document
  seul y est ajouté, non commité.
- Aucun cron, aucun `timeout-minutes`, aucun secret modifié.
- Aucun workflow déclenché, aucun push, aucune migration.
- Je n'ai pas pu vérifier `toJSON(secrets)` en conditions réelles (il faut un
  run GitHub). Deux points à surveiller au premier run, que je signale par
  honnêteté :
  - **le masquage** des valeurs multi-lignes. `BETFAIR_CERT` est un PEM ;
    `toJSON` l'encode avec des `\n` littéraux, une forme que le masqueur de
    GitHub ne reconnaît pas forcément. Nos scripts n'impriment jamais rien, mais
    un traceback qui dumperait l'environnement fuiterait. Ce n'est pas une
    raison de ne pas le faire — c'est une raison de ne jamais ajouter un `echo`
    de debug dans ces steps.
  - **la surface d'exposition** : `SECRETS_JSON` est posé au niveau du **job**,
    donc visible aussi par `actions/checkout` et `pip install`. Une action tierce
    compromise lirait tous les secrets d'un coup au lieu de ceux de son step.
    Le déplacer au niveau de chaque step le corrigerait, au prix d'une
    répétition. Je suis le brief ; je vous le signale.
