# Refonte du périmètre — recentrage sports, efficacité quota 24h/24, boucle d'apprentissage

**Date : 2026-08-22 · Commits : Phase 0 → Phase 4 sur `main` · Suite : 704 tests, 0 échec · pyflakes propre · hook dashboard OK**

Règles tenues à chaque phase : aucun garde-fou modifié (MIN_EDGE/planchers appris, MAX_EDGE, SUSPECT_EDGE,
plancher fiscal, bande Kelly 0.10–0.15, purges, circuit breaker, RLS) ; zéro perte de données (aucune
migration, aucune suppression de ligne `signals`/`ai_learning_ledger`/`meta`) ; plus aucun sport pricé par
recherche web IA à la fin ; dégradation gracieuse conservée partout (retour `[]` + log).

---

## 1. Changements par phase

### Phase 0 — retrait des sports bruités (`refactor(sports)`)
- **Retirés : eSports, tennis de table, volleyball, handball** — leur prix de référence venait d'une recherche
  web IA, jamais d'un book sharp.
- Supprimés : `fetch_esports_events`, `fetch_alternative_sports_batch` (harvester), leurs blocs/imports/TTL
  de cache dans `run_engine.py`, `ALT_SEARCH_MAX_TOKENS` (code + `guerrilla.yml`), entrées mortes de
  `KELLY_FRACTION`, `SPORT_DEFAULTS`, `SPORT_EMOJI`, `_NO_ODDSAPI_SPORTS`, `matchbook.SPORT_IDS`, carte
  marchés OddsAPI.
- Ajouté : `RETIRED_SPORTS` (`core/constants.py`) + **garde dans `_emit`** — plus jamais un signal pour ces
  sports, même depuis un cache meta résiduel, un slate REPRICE ou un harvest tiers.
- **Settlement des lignes historiques inchangé** (test : aucune référence à `RETIRED_SPORTS` dans la chaîne
  settlement/audit/ledger ; `determine_outcome` règle une ligne tabletennis).
- Budget Groq/Tavily libéré ≈ 5 000 tokens réservés/run × ~10 runs payants/jour ≈ **50k TPD** sur 100k par
  clé — réaffecté (commentaire `SEARCH_MAX_TOKENS`, `core/harvester.py`) d'abord au **settlement** (même TPD,
  c'est lui qui manquait le 2026-08-02), puis aux lots `fetch_pinnacle_prices`. Pas de nouveau consommateur.
- Tests : `tests/test_retired_sports.py` (16).

### Phase 1 — MMA + boxe sur flux OddsAPI réel (`feat(sports)`)
- `SPORT_KEYS` : `mma_mixed_martial_arts → mma`, `boxing_boxing → boxing` ; `_MARKETS_BY_SPORT` : h2h seul.
- `fetch_mma_events` (recherche web Melbet/Pinnacle) supprimé avec son bloc, son cache meta et son TTL ;
  `_NO_ODDSAPI_SPORTS` est désormais vide.
- Closing line : `capture_from_scan`/`closing_price_for` couvrent le h2h de tout sport du flux — test dédié
  (signal MMA → prix Pinnacle de la sélection). C'est ce qui rend l'edge MMA (+37,5 % sur 8 paris) validable
  par CLV réel.
- Pré-vol : **0 requête `/odds` quand le slate est vide** (test) — les semaines sans carte ne coûtent rien.
- Tests : `tests/test_mma_boxing_oddsapi.py` (8).

### Phase 2 — NFL, LdC/UEL, Euroleague (`feat(sports)`)
- `americanfootball_nfl → americanfootball`, `soccer_uefa_champs_league`/`soccer_uefa_europa_league → soccer`,
  `basketball_euroleague → euroleague_basketball` (nouveau sport-type : marchés, poids de consensus, elite
  edge, cap SUSPECT = basketball ; Kelly dédiée).
- **Pas de présaison NFL** : `SEASON_OPENS` (`core/odds_api.py`, défaut `2026-09-10`, env `NFL_SEASON_START`)
  — avant cette date, pas même l'appel de pré-vol (test).
- Aucun sport actif retiré (test nominatif des 16 clés existantes). Invariant des 4 fichiers (SPORT_KEYS /
  KELLY_FRACTION / SPORT_DEFAULTS / quotas) vérifié par test. Dashboard, rapport, wiz, settlement couvrent
  les nouveaux types (hook smoke-test OK).
- Tests : `tests/test_new_sports_phase2.py` (9).

### Phase 3 — efficacité quota (`feat(quota)`)
- Nouveau `core/scan_windows.py` : carte des fenêtres favorables (UTC) + `SpendPolicy`, consultée par
  `fetch_odds` ligue par ligue **après** le pré-vol gratuit. Règles : en fenêtre favorable → payé ; sport avec un
  signal actif à < `CLOSING_LINE_WINDOW_MIN` (240 min) du coup d'envoi → payé (closing line prioritaire) ;
  sinon **N = 180 min** minimum entre deux scans payants de la même ligue (= cadence engine ; un tick golden
  hour entre deux ne repaie plus une ligue au repos) ; sous `ODDS_API_RESERVE_CREDITS` (60 ≈ 3 scans favorables)
  le fond s'espace seul. **Chaque ligue sautée est loggée** (`DÉPENSE | …`). `x-requests-remaining` suivi
  (`pool_remaining`). `CLOSING_LINE_BUDGET` intact.
- `engine.yml` : 8 scans/jour replacés sur les fenêtres (02/06/09/12/17/19/21/23 UTC).
- Tests : `tests/test_scan_windows.py` (19).

### Phase 4 — boucle d'apprentissage et d'auto-calibration (`feat(learning)`)
- `compute_and_save` tourne déjà à **chaque audit (6h)** — `core/audit_engine.py:549` ; vérifié, rien à changer.
- `rank_sports.yml` : **cron hebdo lundi 07:00 UTC** (classement + `calibration_report.py` + nouveau
  `scripts/weekly_report.py`).
- Verdicts `core/learning_layer.sport_verdict` persistés dans `meta.sport_verdict_<sport>` à chaque audit,
  repris dans `learning_summary` (alerte rapide via `run_rapport`) — **jamais appliqués automatiquement**.
- Tests : `tests/test_sport_verdicts.py` (11).

---

## 2. Budget crédits OddsAPI — AVANT / APRÈS

Tarif : `/odds` facturé **par ligue peuplée** = marchés × régions (`eu`) → 3 crédits (h2h+spreads+totals),
2 (baseball : h2h+totals), 1 (MMA/boxe : h2h). Pré-vol et `/sports` : 0. Plan : **500 crédits/mois/clé**.

### Coût par ligue et par scan (quand la ligue est peuplée)

| Sport-type | Ligues | Crédits/ligue/scan |
|---|---|---|
| soccer (Big 5, LdC, UEL, SA ×5, MLS) | 12 | 3 |
| basketball (NBA, WNBA) / euroleague_basketball | 3 | 3 |
| hockey (NHL) · americanfootball (NFL) · aussierules · rugbyleague | 4 | 3 |
| baseball (MLB, KBO, NPB) | 3 | 2 |
| mma · boxing | 2 | 1 |
| **Total clés** | **24** (était 18) | — |

### Par jour (ordre de grandeur mesuré : 5–7 ligues peuplées par scan 24h hors saison européenne)

| Poste | AVANT (22/08 matin) | APRÈS |
|---|---|---|
| engine | 12 scans × ~15 cr = **180** | 8 scans sur fenêtres × ~15 cr = **120** |
| deep scan | 4 × ~20 = **80** | 2 × ~20 = **40** |
| golden hour (24 ticks, fenêtre 2h) | 24 × ~4 = **96** | fond espacé 180 min hors fenêtre : ~24 × ~2 = **48** |
| guerrilla (0 OddsAPI) | 0 | 0 |
| REPRICE (gratuit, Matchbook) | — | **0** (24 ticks) |
| **Total/jour** | **≈ 356** → 1 clé dure ~1,4 j | **≈ 208** → 1 clé ~2,4 j ; pool de 4 clés ≈ 10 j |

### Par fenêtre favorable (coût d'UN scan complet quand tout est peuplé)

| Fenêtre UTC | Ligues | Crédits/scan |
|---|---|---|
| 06–13 KBO/NPB (+AFL 02–12, NRL 05–12) | 2 baseball + 2 AU | 4 + 6 = 10 |
| 17–22 Big 5 + LdC/UEL (+Euroleague jeu/ven) | 7 soccer (+1) | 21 (+3) |
| 21–02 Amérique du Sud + MLS + MLB | 5 soccer + 1 baseball | 17 |
| 22–04 NBA/WNBA/NHL | 3 | 9 |
| NFL (ven 00–04, dim 16–24, lun 00–04) | 1 | 3 |
| MMA/boxe (ven–dim) | 2 | 2 |

**Lecture honnête.** À cette profondeur, une couverture 24h/24 ne tient pas sur une clé gratuite : il faut
un pool (`ODDS_API_KEYS`, 4 clés ≈ 10 jours) — ou accepter que la couverture hors fenêtre soit assurée par le
step **REPRICE** (gratuit, horaire) et par golden hour espacé. La politique de dépense ne supprime aucun scan
utile : elle déplace les crédits vers les fenêtres où la ligne bouge et vers la capture de closing line.

---

## 3. Carte des crons 24h (UTC)

| Heure | Workflow(s) | Rôle |
|---|---|---|
| H+00 (10 min) | closing_line | capture de clôture (budget inchangé) |
| H+25 | golden_hour (×2 steps) | scan T-2h (fond espacé hors fenêtre) + **REPRICE** gratuit |
| 02:03 | engine | SA fin, MLB late, NBA/NHL, cartes MMA (ven-dim) |
| 05:33 | deep_scan | KBO/NPB nuit + SA pré-journée |
| 06:03 · 09:03 | engine | KBO/NPB (lag Asie) + AFL/NRL |
| 09:47 · 21:47 | guerrilla | sans OddsAPI (soft gratuit + Matchbook) |
| 12:03 | engine | scan de fond midi |
| 17:03 · 19:03 | engine | Big 5 + LdC/UEL (+ Euroleague jeu/ven) |
| 17:33 | deep_scan | Copa soirée + NBA/NHL T-5h |
| 21:03 · 23:03 | engine | SA + MLB + Big 5 clôture ; NBA/NHL tip-off |
| */6h | audit | settlement + CLV + `compute_and_save` (+ verdicts) |
| */2h H+15 / H+35 | wiz / rapport | analyse contextuelle / rapport Telegram |
| lun 07:00 | rank_sports | classement + calibration + **rapport hebdo de vérité** |

---

## 4. Nouvelles entrées Kelly (justifications)

| Sport-type | Fraction | Justification |
|---|---|---|
| `mma` | 0.08 → **0.10** | le prix de référence devient un vrai Pinnacle (OddsAPI) ; reste sous les majeurs tant que le CLV réel n'a pas tranché (+37,5 % sur 8 paris seulement) |
| `boxing` | **0.08** | marché mince, jamais validé dans le ledger — réévaluer après 30 réglés |
| `americanfootball` | **0.14** | sharpness niveau NBA ; un cran sous la NBA le temps que le ledger confirme |
| `euroleague_basketball` | **0.12** | mécaniques basketball mais marché moins sharp que la NBA — n'hérite pas du 0.15 |
| retirés | — | `esports`, `tabletennis`, `volleyball`, `handball` supprimés de la carte |

---

## 5. Boucle de calibration — critères chiffrés

- **Cadence** : seuils `threshold_<sport>` recalculés à chaque audit (6h) par la learning layer (gate Wilson,
  `_MIN_SAMPLES=20` — valeur existante, conservée ; outcomes réels, jamais `clv_final`) ; rapport hebdo le lundi.
- **Promotion** (`promotion_eligible`) : **≥ 30 signaux réglés en zone jouable** ET **borne basse de Wilson (95 %)
  > rentabilité post-taxe** (`p_breakeven` à la cote moyenne + `TAX_RATE`) → éligible à la restauration
  **progressive** de la fraction Kelly d'origine (un cran par cycle hebdo, décision opérateur).
- **Rétrogradation** (`retrait_propose`) : ≥ 30 réglés et **borne haute < rentabilité** (perte prouvée) ou IC à
  cheval (edge non démontré) → ligne d'alerte dans `learning_summary` (reprise par le rapport Telegram 2h) +
  proposition de retrait dans le rapport hebdo. Jamais de retrait automatique.
- **Métriques de vérité** (rapport hebdo) : CLV réel moyen par sport + part de captures positives, Brier de
  `sharp_prob` + référence, ROI net taxe (pondéré Kelly), taux `SUSPECT_DATA`, verdict. Objectif opérationnel :
  **CLV > 0 et calibration stable**, pas un ROI court terme.
- Note : la mission cite `_MIN_SAMPLES=30` ; la valeur en place est **20** depuis le 2026-08-06 (raison
  documentée dans le code). Conformément à la règle « conserver les réglages », elle n'a pas été touchée ; la
  barre à 30 s'applique aux verdicts de promotion/retrait (`_PROMOTION_MIN_SAMPLES`), qui portent sur la
  taille des mises, pas sur les planchers.

---

## 6. Ce qui reste à l'opérateur
1. Pool OddsAPI : la clé unique est à 499/500 — `python scripts/rotate_odds_key.py --add <clé>` (×3-4).
2. Activer `NFL_SEASON_START` si la date réelle diffère du 10/09.
3. Décider, sur le rapport hebdo, des promotions Kelly et des retraits proposés.


---
---

# Mission 2 — nettoyage dashboard, quota, capacité IA (2026-08-22)

**Commits : `fix(oddsapi)` résolution des clés · `feat(dashboard)` Phases 1-2 · `feat(ai)` Phase 3 · Suite : 723 tests, 0 échec · pyflakes propre · hook dashboard OK (×3).**

## Préalable — « l'ancien OddsAPI, on a réglé ça hier » : vérifié, et ce n'était pas visible du moteur
Constat live (22/08 03:00 UTC) : `app_secrets.ODDS_API_KEY` = `…077b` (maj **06/08**), 499/500, et le
scan engine de 01:43 UTC se terminait encore sur `HTTP 401 → pool épuisé`. Cause : `get_secret()` ne
regarde l'environnement QUE si la table est vide — une clé neuve posée dans les secrets GitHub restait
invisible tant que la valeur périmée était dans la table. Correctif (`candidate_keys`) : l'env
(`ODDS_API_KEYS`, `ODDS_API_KEY`) rejoint TOUJOURS le pool après la table (la clé morte est écartée par la
sonde gratuite, la neuve prend le relais), et les workflows engine/deep/golden transmettent aussi
`ODDS_API_KEYS`. À vérifier sur le prochain run : la ligne « OddsAPI clé #k/N active (…xxxx) ».

## Phase 1 — vues modifiées
| Vue / composant | Avant | Après |
|---|---|---|
| `/performance` — héros win rate + KPIs | tout le ledger (500 lignes) | lignes filtrées par `core/perf_view.filter_rows` : sports ∉ `RETIRED_SPORTS` **et** mois ∈ `PERF_MONTHS_SHOWN` (défaut 2) |
| `/performance` — « Par sport » | incluait tabletennis/esports/… | retirés absents |
| `/performance` — « Calibration » (Brier) | idem | calculée sur les lignes filtrées |
| `/performance` — « Par mois » | tous les mois | N derniers mois, libellé « (les N derniers — PERF_MONTHS_SHOWN) » |
| `/performance` — « Historique » | idem | lignes filtrées |
| `scripts/rank_sports.py`, `scripts/calibration_report.py` | boucle `SPORT_DEFAULTS` | garde explicite `RETIRED_SPORTS` |
| `sql/archive_retired_sports.sql` | — | archivage MANUEL (jamais par workflow) vers `ai_learning_ledger_archive` (+`archived_at`), réversible, delete borné aux lignes copiées |

Rien n'est supprimé : filtre d'affichage pur, testé sans rendu de template (`tests/test_mission2_dashboard_quota.py`).

## Phase 2 — comportement d'alerte quota (capture)
Widget « Quota OddsAPI » et `/api/odds-quota` retirés (page Sys). La surveillance reste backend :
```
INFO    | Quota OddsAPI : 20 restantes / 500 (4.0%) — clé active
Telegram → 🔴 *OddsAPI : pool sous 5%* — 20 crédits restants sur 500 (4.0%).
           Fenêtres favorables et closing line restent prioritaires ; le fond s'espace (core/scan_windows).
           Ajouter une clé : `python scripts/rotate_odds_key.py --add <clé>`
[run suivant, même palier] INFO | Alerte [alert_oddsapi_pool_5] déjà envoyée il y a 1.0h — silence
```
Paliers 20 % (`alert_oddsapi_pool_20`) et 5 % (`alert_oddsapi_pool_5`), **une alerte par palier et par
24 h** (dédup `meta` via `_alert_once`). Test : pool à 4 % → exactement une alerte, silence au run suivant ;
18 % → palier 20 % ; 80 % → log seul. La réserve journalière de la Mission 1 lit la même mesure
(`pool_counters`, alimentée par la sonde gratuite et chaque réponse payante).

## Phase 3 — fournisseurs IA : ordre de repli et quotas
| Rang | Fournisseur | Rôle | Quota (gratuit, 1 compte) | Suivi | Env |
|---|---|---|---|---|---|
| 1 | Groq `groq/compound-mini` | recherche web intégrée | TPD 100k tokens/jour par org (modèle 70b) | par modèle × clé (process) | `GROQ_API_KEY` (+`_2.._4` si autres orgs) |
| 1' | Groq `llama-3.3-70b` / `llama-3.1-8b` | extraction / complétion (paliers heavy/light) | 100k TPD 70b ; 8b plus large | idem | idem |
| 2 | Tavily | snippets web (étage 2) | 1 000 crédits/mois | budget/run 25 | `TAVILY_API_KEY` |
| 3 | OpenRouter (`…:free`) | repli complétion | ≈ 200 req/jour free | `meta.quota_ai_openrouter` (150) | `OPENROUTER_API_KEY` |
| 4 | Cerebras | repli complétion | ≈ 1M tokens/jour free | `meta.quota_ai_cerebras` (500) | `CEREBRAS_API_KEY` |
| 5 | GitHub Models | repli complétion | ≈ 150 req/jour (low tier) | `meta.quota_ai_github` (100) | `GITHUB_MODELS_TOKEN` |
| — | Mistral | **hors chaîne** | — | — | domaine de panne Wiz, par construction |
| — | Gemini | **mort** en gratuit | — | — | — |

Réduction de consommation : cache de réponses 30 min (`meta.ai_cache_<hash>`, requête normalisée) — le
même slate n'est jamais recherché deux fois ; palier `light` (8b d'abord) pour l'estimateur, `heavy` pour
le settlement ; `SEARCH_MAX_TOKENS` réévalué (2048 reste le plancher sûr d'un lot Pinnacle de 25).

## OddsAPI — payant vs gratuit optimisé (décision opérateur)
Tarifs constatés (the-odds-api.com, 22/08) : Starter **gratuit 500 crédits/mois** · **20K = 30 $/mois** ·
100K = 59 $/mois · 5M = 119 $/mois.

| Scénario | Crédits/mois | Couverture |
|---|---|---|
| Gratuit, 1 clé, rythme d'avant Mission 1 (≈356/j) | 500 | ≈ 1,4 jour |
| Gratuit, 1 clé, crons optimisés (≈208/j ≈ 6 240/mois) | 500 | ≈ 2,4 jours |
| Gratuit, pool de 4 comptes (exclu par la règle « un compte par fournisseur ») | 2 000 | ≈ 10 jours |
| **Payant 20K (30 $/mois)** | 20 000 | ≈ 3,2 × le besoin optimisé — marge pour deep scans et nouvelles ligues |
| Payant 100K (59 $/mois) | 100 000 | surdimensionné au périmètre actuel |

Lecture : à 6 240 crédits/mois après optimisation, **le palier 20K (30 $) couvre le besoin avec 3× de marge** ;
le gratuit ne tient que 2-3 jours par clé et la règle « un compte par fournisseur » interdit le pool de
comptes. Le step REPRICE (Matchbook, gratuit) couvre la fraîcheur hors fenêtre mais pas la découverte du
slate Tier 1. Recommandation : 20K si l'opérateur veut le Tier 1 en continu ; sinon rester gratuit et
accepter une découverte Tier 1 partielle, portée par api-sports/odds-api.io/Titan007 + Matchbook.

---

# Mission 3 — nouvelles sources de cotes gratuites (Asie incluse) (2026-08-22)

**Suite : 808 tests, 0 échec · pyflakes propre · 85 tests ajoutés · migration `sql/migrate_v10_3_team_aliases.sql` (additive, à appliquer à la main)**

Règle tenue : **aucune source n'est intégrée sur une supposition**. Chaque ligne du tableau
« preuve de vie » ci-dessous est un `curl` réel, passé depuis le runner (IP datacenter Azure) le
2026-08-22, avec le User-Agent de production. Trois des six sources du cahier des charges ont été
écartées sur la foi de ces mesures, et le rapport dit pourquoi.

## 1. Preuve de vie — depuis le runner, le 2026-08-22

Contrôle de calibration d'abord : `bf.titan007.com` (source connue vivante) répond **200**, et
`www.matchbook.com/edge/rest/events` **200** depuis la même sortie. Les échecs ci-dessous sont donc
des échecs réels, pas un réseau muet.

### Sources retenues

| Source | Endpoint (sans query string) | Code | Taille | Extrait de réponse |
|---|---|---|---|---|
| **500.com** | `odds.500.com/` | **200** | 288 418 o | `<title>【足球指数】…500彩票网</title>` — 64 matchs, `data-fid` + `date-dtime` |
| **500.com** | `odds.500.com/fenxi/ouzhi-1420317.shtml` | **200** | 275 767 o | `<title>赫尔城VS曼彻斯特联(2026/2027英超)-百家欧指</title>` — 30 books |
| **500.com** | `odds.500.com/fenxi/yazhi-1420317.shtml` | **200** | 189 228 o | 亚盘 (handicap asiatique) |
| **500.com** | `odds.500.com/fenxi/daxiao-1420317.shtml` | **200** | 190 833 o | 大小球 (totals) |
| **7M** | `www.7msport.com/sitemap/soccer_match.xml` | **200** | 132 129 o | 936 identifiants `goaldata/en/{id}.shtml` |
| **7M** | `px-analyse.7mdt.com/5170957/data/gameinfo_en.js` | **200** | 444 o | `{"time":"1787407200000","taname":"Broadfields United","tbname":"Corinthian FC","mname":"England FA Cup"}` |
| **Kalshi** | `api.elections.kalshi.com/trade-api/v2/markets?series_ticker=KXEPLGAME` | **200** | 4 596 o | `KXEPLGAME-26AUG29TOTNEW-TOT`, `no_ask_dollars:"0.5800"` |
| **Polymarket** | `gamma-api.polymarket.com/events?tag_slug=epl` | **200** | 211 140 o | `epl-hul-mun-2026-08-22` → `["0.095","0.185","0.715"]` |

### Sources écartées — et la mesure qui les écarte

| Source | Mesure | Verdict |
|---|---|---|
| **Nowgoal / win007** (n° 2 du cahier des charges) | `www.nowgoal.com` **000** (pas de résolution DNS) · `live.nowgoal.com` **000** · `football.nowgoal.com` **000** · `bf.win007.com` **000** · `www.win007.com` **000** · `1x2.nowgoal.com` **000** · `www.nowgoal5.com` **301 → canadafbm2020.com → 403** | **Morte.** Toute la famille est injoignable depuis une IP datacenter ; le seul hôte qui répond redirige vers un domaine parqué qui rend 403. Aucune ligne de code écrite. |
| **Betman / 프로토 (wisetoto)** (n° 4) | `www.wisetoto.com/` **200** mais `/proto/index.htm`, `/proto/`, `/rate/index.htm` → **302 vers `/errorpage/404.htm`** · `www.betman.co.kr` **000** | **Non codée**, conformément à la règle du cahier des charges (« priorité basse, n'y passer du temps que si 1 et 2 sont en prod ») : la n° 2 est morte, la condition n'est pas remplie. L'hôte vit, mais aucun endpoint de cotes n'a été trouvé. |
| **Japon WINNER/toto** (n° 6) | — | **Non codée**, comme demandé. |

### Le cas 7M : vivante, mais pas pour ce qu'on venait chercher

7M était retenue comme **redondance de cotes sur un hôte différent**. Cette redondance **n'existe
pas** : il n'y a aucun endpoint de cotes gratuit.

| Endpoint testé | Code | Contenu réel |
|---|---|---|
| `px-analyse.7mdt.com/{id}/data/gameoddsway_en.js` | 200 | **pas des prix** — statistiques historiques de résultats par book |
| `px-analyse.7mdt.com/{id}/data/gameodds_en.js` | **500** | — |
| `…/gameoz_en.js` (欧赔) · `…/gameyp_en.js` (亚盘) · `…/gamedx_en.js` (大小球) | **500** | — |
| `data.7msport.com/matches_data/odds_way_en.shtml` | 200 | « Stat. on Payout », pas de cotes par match |

**Mais l'exploration a trouvé mieux pour un autre problème.** `gameinfo_en.js` publie le même
calendrier **en anglais**, avec identifiants numériques d'équipe et horodatage epoch. C'est
exactement ce qui manquait au dictionnaire d'alias : 7M est donc intégrée avec le rôle `names` et
remplace l'appel IA prévu au cahier des charges par une résolution **gratuite et déterministe**.
C'est une réaffectation, pas un abandon — et elle est documentée en tête de `core/sevenm.py`.

## 2. Statut juridique constaté, source par source

| Hôte | `robots.txt` | Ce qu'il dit | Conséquence tenue dans le code |
|---|---|---|---|
| `odds.500.com` | **200** | Interdit `/fenxi1/`, `/js/`, `/static/`, `/images/`, les motifs **ancrés racine** `/ouzhi-*.shtml`, et les variantes **paramétrées** `/fenxi/ouzhi-*.shtml?ctype=*`, `?order=*`, `?cids=*` | Les 3 endpoints utilisés sont sous `/fenxi/` **sans query string** → hors Disallow. Le lien `/fenxi1/ouzhi_same.php?cid=…` présent dans chaque ligne de la page n'est **jamais** suivi. |
| `www.7msport.com` | **200** | **Une seule ligne : `Sitemap:`** — aucun Disallow | Les identifiants viennent du sitemap, c'est-à-dire du chemin que le site publie *pour* être moissonné. |
| `data.7msport.com` | **200** | Interdit les variantes de langue `/*_gb.shtml`, `_kr`, `_jp`, `_th`, `_vn`, `_fr`, `_id`, `_es` | On n'utilise que la variante **anglaise** `/goaldata/en/`, hors de ces motifs. |
| `px-analyse.7mdt.com` | **200** | Bloc Cloudflare : `User-agent: *` → `Content-Signal: search=yes,ai-train=no,use=reference` + **`Allow: /`**. Puis `Disallow: /` nommément pour ClaudeBot, GPTBot, CCBot, Google-Extended, Bytespider, Amazonbot… | Le pipeline tombe sous `User-agent: *` → autorisé, et son usage est bien `use=reference` (aucun entraînement de modèle). **Condition : le User-Agent doit rester honnête** et ne se faire passer pour aucun agent nommé. |
| `api.elections.kalshi.com` · `gamma-api.polymarket.com` | n/a (API) | API publiques documentées, lecture seule, sans clé | Le statut le plus propre du lot : aucun scraping. |
| `trade.500.com` | **200** | Longue liste de Disallow **tous paramétrés** (`/jczq/?`, `…?lotid=*`, `…?step=*`) | Hôte non utilisé ; sa liste confirme la doctrine « la query string est la frontière ». |

**La règle héritée de titan007 (« ne jamais ajouter de paramètre à un endpoint ») n'est plus une
prudence sur cette source : c'est littéralement le texte du robots.txt d'`odds.500.com`.** Le même
chemin est autorisé nu et interdit avec `?ctype=`. C'est verrouillé par
`tests/test_odds500.py::TestRobotsTxt`, qui garde une copie du robots.txt publié et vérifie la
bascule.

> **Correction d'une observation transitoire.** Au premier passage, `odds.500.com/robots.txt`
> répondait **403** et `live.500.com` / `www.500.com` répondaient **567 « Restricted Access »**
> (WAF Tencent EdgeOne). Quelques minutes plus tard, les trois servaient un `robots.txt` normal en
> **200**. C'était un blocage transitoire du WAF, pas une politique : aucune conclusion juridique
> n'en est tirée. `live.500.com` et `www.500.com` restent simplement hors périmètre parce qu'ils
> n'apportent rien de plus qu'`odds.500.com`.

## 3. Tableau des adaptateurs

| Adaptateur | Rôle | Trust initial | Budget/jour | Langue | Cadence | Hôte |
|---|---|---|---|---|---|---|
| `core/odds500.py` | `consensus` (→ `sharp` après promotion) | **0,55** | 400 req | `zh` | 1 req / 2 s | `odds.500.com` |
| `core/sevenm.py` | `names` | **0,70** | 80 req | `en` | 1 req / 2 s | `px-analyse.7mdt.com` |
| `core/prediction_markets.py` (Kalshi) | `consensus` | **0,50** | 200 req (partagé) | `en` | API | `api.elections.kalshi.com` |
| `core/prediction_markets.py` (Polymarket) | `consensus` | **0,50** | 200 req (partagé) | `en` | API | `gamma-api.polymarket.com` |

Trust initial **effectivement divisé par deux tant que la source est en mode ombre**
(`source_adapter.effective_trust`). Aucune ne démarre en `sharp` : 500.com ne peut y prétendre
qu'après promotion mesurée (§ 6).

### Carte des books de 500.com — identifiée sans lire un seul nom

Pour un visiteur anonyme, la page de cotes **masque les libellés** (`P*********`, `*冠`, `*门`).
L'identifiant numérique `cid`, lui, est en clair et stable. L'identité de chaque book a été établie
par deux signatures qui n'ont pas de langue — **la marge et le pays** :

| `cid` | Marge 1X2 mesurée | Pays affiché | Identité | Rôle |
|---|---|---|---|---|
| 18 | **0,46 %** | Royaume-Uni | Betfair Exchange | `sharp` (exchange) |
| 1055 | **3,87 %** | Pays-Bas | **Pinnacle** | `sharp` (référence) |
| 3 | 5,71 % | Royaume-Uni | Bet365 | `soft` |
| 280 | 10,26 % | Philippines | 皇冠 / Crown | pseudo |
| 5 | 11,12 % | Macao | 澳门 / Macau | pseudo |

Le calendrier publie par ailleurs **deux libellés en clair** (`Bet365` pour `cid=3`, `澳门` pour
`cid=5`) : `verify_book_map()` s'en sert à chaque run pour détecter gratuitement une renumérotation
des books — sans quoi `BOOK_MAP` deviendrait fausse **en silence** et un prix « Pinnacle » pourrait
être celui d'un book à 13 % de marge, donc des edges massifs et faux.

## 4. Schéma `team_aliases` (`sql/migrate_v10_3_team_aliases.sql`)

Additive, idempotente, RLS activée (lecture publique, écriture `service_role`).

| Colonne | Type | Rôle |
|---|---|---|
| `source` | `text` | Adaptateur qui a vu le libellé (`odds500`, `sevenm`…) |
| `alias_source` | `text` | Libellé **brut** publié par la source (`鹿岛鹿角`) |
| **`source_team_id`** | `text` | **Identifiant numérique stable chez la source — la vraie clé** |
| `lang` | `text` | `zh` / `ja` / `ko` / `en`, détecté par plage Unicode |
| `canonical_name` | `text` | Nom anglais canonique utilisé par le moteur et le ledger |
| `league` | `text` | Ligue au moment de la résolution |
| `confidence` | `float8` | 0..1 — monte à chaque appariement confirmé, tombe à 0 sur contradiction |
| `hits` / `contradictions` | `int` | Compteurs d'auto-validation |
| `resolved_by` | `text` | `sevenm` (gratuit) · `ai` (Groq) · `manual` |
| `verified_at` | `timestamptz` | Dernier appariement indépendant confirmant l'alias |

Index unique sur `(source, alias_source, COALESCE(league,''))`, index de lecture sur
`(source, source_team_id)`.

**Pourquoi `source_team_id` et pas le libellé.** 500.com expose `liansai.500.com/team/1029/` et 7M
expose `taid`. Ces identifiants **n'ont pas de langue** : ils survivent à un changement de graphie
(`曼联` → `曼彻斯特联`), à une abréviation, à un nom de sponsor. Le libellé n'est gardé que pour la
lisibilité et comme repli. C'est la transposition, aux équipes, de la règle d'appariement du
moteur : la structure d'abord, le nom en confirmation.

**Économie du dictionnaire.** Une résolution par nom, **à vie** :
1. `sevenm` — appariement 500.com ↔ 7M par (coup d'envoi ± 15 min, ligue, structure) → traduction
   **gratuite**, confiance de départ 0,70 ;
2. `ai` — Groq, prompt court, **uniquement pour ce que 7M n'a pas couvert**, confiance de départ
   0,40, budget 40 résolutions/jour (`meta.quota_alias_ai_<date>`).

Un alias déjà connu **ne repasse jamais** par l'IA — vérifié par
`test_team_aliases.py::test_un_nom_deja_connu_ne_repasse_jamais_par_lIA`. Le budget bas est
volontaire : le dictionnaire se remplit sur plusieurs jours, ce qui est sans conséquence pour une
donnée qui ne périme pas, alors qu'épuiser le TPD Groq casserait le settlement **le jour même**
(incident du 2026-08-02).

**Auto-validation.** `confidence +0,10` par appariement indépendant confirmé ; **`0,0` immédiat sur
contradiction**. Asymétrie voulue : plusieurs confirmations pour monter, **une seule** contradiction
pour tomber. Un alias faux produit un edge élevé, crédible et entièrement imaginaire ; un alias
écarté à tort ne coûte qu'un match. Seuil d'usage `MIN_CONFIDENCE = 0,60` : un alias 7M passe dès le
premier appariement, un alias IA exige **deux** confirmations indépendantes.

## 4 bis. Preuve d'appariement multilingue — mesurée, pas supposée

Le cœur du cahier des charges (« appariement SANS dépendre des noms ») a été passé **en réel** :
les 64 fixtures `odds.500.com` (libellés chinois) contre les **936** fixtures 7M (libellés anglais)
du sitemap complet, appariées uniquement par **(coup d'envoi ± 15 min, ligue mappée, structure)**.
Aucun libellé n'est comparé à aucun autre, à aucun moment.

**Résultat : 34 paires, 34 correctes (68 alias appris), 0 fausse — vérifiées une à une.**
Couverture : 西甲 8, 葡超 6, 英超 4, 荷甲 4, 法甲 3, 意甲 2, 日职 2, 美职足 2, 瑞超 2, 英冠 1, 巴甲 1.

| 500.com (`zh`) | 7M (`en`) — obtenu sans lire un nom |
|---|---|
| 日职 鹿岛鹿角 / 福冈黄蜂 | Kashima Antlers / Avispa Fukuoka |
| 日职 町田泽维 / 浦和红钻 | FC Machida Zelvia / Urawa Red Diamonds |
| 英超 赫尔城 / 曼联 | Hull City A.F.C. / Manchester United F.C. |
| 英超 纽卡斯尔 / 利物浦 | Newcastle United F.C. / Liverpool F.C. |
| 英超 布伦特 / 热刺 | Brentford F.C. / Tottenham Hotspur F.C. |
| 英超 富勒姆 / 切尔西 | Fulham F.C. / Chelsea F.C. |
| 西甲 西班牙人 / 皇马 | RCD Espanyol / Real Madrid CF |
| 西甲 埃尔切 / 巴萨 | Elche CF / FC Barcelona |
| 西甲 马竞 / 比利亚雷 | Atletico Madrid / Villarreal CF |
| 西甲 毕尔巴鄂 / 塞维利亚 | Athletic Bilbao / Sevilla FC |
| 意甲 罗马 / 佛罗伦萨 | AS Roma / ACF Fiorentina |
| 意甲 博洛尼亚 / 拉齐奥 | Bologna FC 1909 / SS Lazio |
| 法甲 朗斯 / 欧塞尔 | RC Lens / AJ Auxerre |
| 法甲 勒阿弗尔 / 摩纳哥 | Le Havre AC / AS Monaco FC |
| 荷甲 埃因霍温 / 格罗宁根 | PSV Eindhoven / FC Groningen |
| 荷甲 坎布尔 / 费耶诺德 | SC Cambuur / Feyenoord |
| 葡超 波尔图 / 阿罗卡 | FC Porto / FC Arouca |
| 巴甲 博塔弗戈 / 巴竞技 | Botafogo de Futebol e Regatas / Atletico Paranaense |
| 瑞超 马尔默 / 佐加顿斯 | Malmo FF / Djurgardens IF |
| 美职足 新英格兰 / 纽约城 | New England Revolution / New York City FC |

*(20 des 34 ; les 14 autres — 葡超, 西甲, 英冠, 荷甲, 美职足, 瑞超 — sont du même acabit.)*

Le taux d'appariement (34 sur 64 côté 500.com) est plafonné par la **couverture de `LEAGUE_MAP`**,
pas par l'algorithme : une ligue non mappée des deux côtés ne s'apparie pas, par construction.
Ajouter une entrée à la carte ne fait qu'AUTORISER un appariement de plus, jamais l'imposer.

### Le premier passage rendait 16 paires, dont UNE fausse

C'est le résultat le plus utile de la mission. Avant le garde d'ambiguïté, l'appariement rendait
aussi :

```
[英冠] 斯旺西 / 谢菲联  (Swansea / Sheffield Utd)
    ->  Wrexham A.F.C. / Watford F.C.        ← FAUX
```

Les deux rencontres sont en **EFL Championship à la même minute**, et les calendriers ne portent
pas de cotes : le critère (a) temps et le critère (b) ligue ne distinguent rien, et le critère (c)
structure est indisponible. Le tri glouton tranchait **au hasard** — et aurait écrit `斯旺西 →
Wrexham` dans le dictionnaire, à vie.

**Correctif** (`source_adapter.pair_fixtures`) : un appariement doit être *justifié*, pas seulement
*le meilleur*.
- sans signature de cotes → on exige l'**unicité** (un seul candidat de chaque côté) ;
- avec signature → on exige que le second candidat soit à plus de `AMBIGUITY_MARGIN_PTS` (2,0 pts).

Après correctif : **0 fausse paire** sur les 34. Le prix payé est la perte de Millwall/Norwich — une
paire *correcte* mais dont le rival était indiscernable. C'est le bon arbitrage : un match perdu ne coûte
qu'un match, un alias faux empoisonne le dictionnaire et produit un edge crédible et imaginaire.
Verrouillé par trois tests, dont
`test_les_cotes_departagent_ce_que_le_temps_ne_departage_pas` qui montre que les mêmes deux matchs
**redeviennent** appariables dès que les cotes sont disponibles.

## 5. Seuils du cross-check — et pourquoi le « 3 % » du cahier des charges a été remplacé

Le cahier des charges demandait « divergence > 3 % sur le même marché → `SUSPECT_DATA` ». **Mesuré,
ce seuil est inutilisable.** Sur Hull City–Manchester United, trois chemins indépendants
(500.com/Pinnacle, 500.com/Betfair, Polymarket) :

| Comparaison (no-vig) | 1 (outsider, 9,5 %) | X | 2 (favori) |
|---|---|---|---|
| **écart relatif** — 500/Pinnacle vs Polymarket | **9,73 %** | 0,74 % | 1,48 % |
| **écart relatif** — 500/Betfair vs Polymarket | **4,26 %** | 0,86 % | 0,34 % |
| **écart en points** — 500/Pinnacle vs Polymarket | **0,93 pt** | 0,14 pt | 1,07 pt |
| **écart en points** — 500/Betfair vs Polymarket | **0,41 pt** | 0,16 pt | 0,25 pt |

Les trois sources sont **d'accord** — l'écart maximal réel est de **1,07 point de probabilité**.
Le relatif explose sur l'outsider parce qu'un tick d'un cent à 0,095 pèse déjà 1,05 % relatif. Un
seuil relatif à 3 % marquerait `SUSPECT_DATA` sur presque **chaque outsider** — c'est-à-dire
exactement là où ce pipeline trouve ses edges.

**Décision : on garde la magnitude choisie par l'opérateur (2/3), mais en points de probabilité
absolus**, stables sur toute la plage de prix.

| Seuil | Valeur | Variable | Rôle |
|---|---|---|---|
| Divergence → `SUSPECT_DATA` | **3,0 points** | `SOURCE_SUSPECT_PTS` | 2+ chemins vers le même prix divergent → **aucun signal** |
| Distance structurelle max (appariement) | **12,0 points** | `SOURCE_STRUCT_MAX_PTS` | Filtre anti-absurdité, large exprès |
| Marge d'ambiguïté (appariement) | **2,0 points** | `SOURCE_AMBIGUITY_MARGIN_PTS` | En deçà, deux candidats se valent → paire **écartée** |
| Tolérance de coup d'envoi | **± 15 min** | `SOURCE_KICKOFF_TOL_MIN` | Critère (a) de l'appariement |
| Sortie du mode ombre — matchs | **100 appariés** | `SOURCE_SHADOW_MIN_MATCHES` | Condition 1 |
| Sortie du mode ombre — divergence | **médiane ≤ 2,0 points** | `SOURCE_SHADOW_MAX_MED_PTS` | Condition 2 |
| Écart bid/ask max (marchés de prédiction) | **4,0 points** | `PREDMKT_MAX_SPREAD_PTS` | Un carnet plus large que le seuil `SUSPECT` n'apprend rien |
| Marge max du panel pseudo-sharp | **6,0 %** | `ODDS500_PSEUDO_MAX_VIG` | Sélection **par marge mesurée** |
| Pénalité pseudo-sharp | **+1,0 %** | `ODDS500_PSEUDO_PENALTY` | Gonfle la référence → **réduit** l'edge (mécanique `core/oracle.py`) |

**Médiane et non moyenne** pour la promotion : un seul prix périmé ne doit ni bloquer une bonne
source, ni sauver une mauvaise. **Rétrogradation immédiate** si une source promue dérive au-dessus
de 2,0 points — asymétrie symétrique de celle des alias.

### Correction au pseudo-sharp du cahier des charges

Le cahier des charges proposait la médiane no-vig de **{皇冠/Crown, Bet365, 澳门/Macau}**. Mesuré,
ce trio porte **5,71 % / 10,26 % / 11,12 %** de marge sur le 1X2 : 皇冠 et 澳门 sont des books de
**handicap asiatique** dont le 1X2 est décoratif. Les prendre comme référence sharp reviendrait à
calculer un edge contre un prix chargé à 11 %.

`pseudo_sharp_price()` sélectionne donc les books **par marge mesurée sur le match courant**
(≤ 6 %), pas par liste écrite d'avance. C'est le même critère — « prendre les books sharps » —
appliqué à la donnée du jour. Le gain est concret : sur un autre match, Crown est ressorti à
**4,22 %** de marge et a été **inclus** ; une liste figée l'aurait soit toujours pris, soit toujours
exclu. Médiane des **probabilités dévigorisées** (grandeur additive), jamais des cotes — une médiane
de cotes ne somme pas à 1 et fabriquerait une marge parasite.

## 6. Mode ombre et ordre d'appel

**Mode ombre obligatoire.** Toute nouvelle source démarre en `shadow=True` : ses prix sont
enregistrés et comparés, mais **ne créent aucun signal misable** tant qu'elle n'a pas **100 matchs
appariés avec une divergence médiane ≤ 2,0 points** face à une source de confiance. On ne coupe pas
la collecte — on retire la recommandation, exactement comme `tests/test_shadow_mode.py` : couper le
cron aurait aussi arrêté la mesure, et on n'aurait jamais su si la source est bonne. Promotion et
rétrogradation **loggées dans `meta.source_scorecard_<name>`**, jamais silencieuses.

**Scorecard par source** (`meta`, fenêtre glissante de 300 observations) : fraîcheur médiane
(gratuite — 500.com publie `data-time` **par book**), taux d'erreur, nombre de requêtes, divergence
médiane vs source de confiance.

**Ordre d'appel par scan** (`source_adapter.CALL_ORDER`) — ces sources **s'ajoutent** à la discipline
quota de la mission 1, elles ne la remplacent pas :

```
odds_api (si crédits) → odds500 → titan007 → matchbook → api_sports
  → odds_api_io → prediction_markets → web_search (dernier recours, budget IA)
```

## 7. Tests ajoutés (85)

| Fichier | Tests | Ce qui est verrouillé |
|---|---|---|
| `tests/test_source_adapter.py` | 33 | **Appariement multilingue** (chinois ↔ canonique) sans comparer un libellé · **divergence → `SUSPECT_DATA`** · démonstration chiffrée que le seuil relatif à 3 % était inutilisable · mode ombre et rétrogradation · **garde d'ambiguïté** (régression de la fausse paire Swansea→Wrexham) |
| `tests/test_odds500.py` | 25 | Parseurs sur **HTML réel capturé** · fuseau UTC+8 · carte des books et détection de renumérotation · pseudo-sharp par marge mesurée · **conformité `robots.txt` publié** |
| `tests/test_team_aliases.py` | 12 | Apprentissage 鹿岛鹿角 → Kashima Antlers · confirmation · invalidation immédiate · seuils de confiance · budget IA borné · dégradation sans base |
| `tests/test_prediction_markets.py` | 15 | Champs `*_dollars` (les entiers sont `null`) · chaînes JSON de Polymarket · rejet d'un 1X2 amputé · concordance avec 500.com |

Les deux tests exigés par le cahier des charges :
`test_source_adapter.py::TestAppariementSansLesNoms::test_chinois_et_anglais_sapparient` et
`test_source_adapter.py::TestDivergenceEtSuspectData::test_un_prix_perime_declenche_suspect_data`.

## 8. Bugs trouvés par les tests, avant la production

1. **User-Agent non-ASCII → 403 silencieux.** L'ancien UA contenait « usage privé ». `urllib` encode
   les en-têtes en **latin-1** ; Cloudflare rendait **403** sur `gamma-api.polymarket.com` là où
   `curl` passait. Une source morte pour un accent — indiagnosticable depuis un log de cron.
   Corrigé sur les trois adaptateurs, verrouillé par un test.
2. **`novig_probs([0.0, 3.0, 2.0])` rendait une signature à 2 issues.** Un 1X2 amputé de son nul
   devenait indiscernable d'un moneyline, donc **appariable avec lui** par `structure_distance` —
   le scénario qui lie des cotes au mauvais match sans rien logger. Garde corrigé.
3. **Kalshi : un 1X2 amputé passait pour un moneyline.** Une patte écartée pour carnet trop large
   laissait `[Brentford, Tie]`. Même risque que ci-dessus. On exige désormais les trois pattes, ou rien.
4. **Appariement ambigu tranché au hasard → alias faux.** Trouvé en confrontant réellement les deux
   calendriers (§ 4 bis), pas en relisant le code : deux matchs d'une même ligue à la même minute,
   sans cotes, étaient appariés arbitrairement. C'est la seule erreur de ce pipeline qui produise un
   edge élevé, crédible **et entièrement imaginaire**. Garde d'unicité/marge ajouté.

## 9. Ce qui reste à l'opérateur

1. **Appliquer `sql/migrate_v10_3_team_aliases.sql`** dans le SQL Editor Supabase (aucun runner de
   migration dans ce dépôt). Sans cette table, le dictionnaire dégrade proprement (mémoire du run
   seulement) mais ne persiste rien.
2. **Brancher les adaptateurs dans `run_engine.py` / `core/harvester.py`.** Les modules, le cadre
   commun et les tests sont livrés ; le câblage dans le scan n'est **pas** fait — c'est un
   changement du chemin critique d'émission des signaux, qui mérite sa propre revue et son propre
   passage en mode ombre observé.
3. **Décider du sort de wisetoto** (n° 4) : l'hôte vit, mais aucun endpoint de cotes n'a été trouvé,
   et sa condition d'activation (« si 1 et 2 sont en prod ») n'est pas remplie puisque Nowgoal est morte.

---

# Mission 4 — couche IA multi-fournisseurs, organisée en symbiose (2026-08-22)

**Suite : 844 tests, 0 échec · pyflakes propre · 44 tests ajoutés · `core/ai_router.py` (nouveau) · `.env.example` enrichi de 11 clés optionnelles**

Règle non négociable tenue : **UN compte par fournisseur**. La capacité vient de la DIVERSITÉ des
fournisseurs, jamais de comptes multiples — qui partageraient de toute façon le quota (vérifié sur
Groq : le TPD est compté par organisation) et violeraient les CGU. Les fournisseurs à clause
« non commercial / évaluation » portent un `terms_flag` et sont exclus des lanes de production par
défaut.

## 1. Le principe directeur, et pourquoi il n'est pas théorique

Le paysage des paliers gratuits churne chaque mois. Les deux corrections demandées ont été
**vérifiées sur le fil**, pas admises :

| Fournisseur | Requête | Réponse | Verdict |
|---|---|---|---|
| GitHub Models | `GET models.github.ai/catalog/models` | **HTTP 410** `{"code":"github_models_retirement_brownout"}` | **Retiré** — sorti du code |
| Cerebras | `GET api.cerebras.ai/v1/models` | **HTTP 403** `{"detail":"Not authenticated"}` | Palier gratuit sans carte **fermé** — sorti du code |

### Et une troisième mort, trouvée en chemin — celle qui prouve le besoin

En lisant le registre existant de `core/ai_search.py`, un troisième cadavre est apparu, celui-là
**non signalé et actif en production** :

```python
OPENROUTER_MODEL = "meta-llama/llama-3.3-70b-instruct:free"   # ← n'existe plus
```

Le catalogue OpenRouter du 2026-08-22 compte **421 modèles, dont 18 en `:free`** — et celui-ci n'y
est plus. Seule la variante **payante** `meta-llama/llama-3.3-70b-instruct` subsiste. Le repli
OpenRouter était donc mort, et mort **en silence** : l'appel partait, le fournisseur rendait une
erreur de modèle inconnu, le code loggait un avertissement et passait au suivant. Rien ne
distinguait « le repli n'a pas servi » de « le repli ne peut plus servir ».

Constat plus large : **aucun** des modèles cités au cahier des charges (Llama 70B, Qwen3 235B,
GLM-4.5-Air, DeepSeek) n'est encore en `:free` chez OpenRouter. La liste de la mission était déjà
périmée le jour où elle a été écrite. C'est la démonstration la plus nette possible du principe :
**l'architecture — registre, découverte, bascule, alerte — vaut plus que la liste.**

Traduction en code : `models` est une **liste de préférences**, jamais un contrat. `resolve_model()`
retient la première préférence effectivement présente au catalogue publié par le fournisseur au
moment du run.

## 2. Registre — quotas et catalogues CONSTATÉS EN LIVE le 2026-08-22

Toutes les lignes ci-dessous sont un `GET /models` réel depuis le runner. `401` = vivant et
authentifié par clé (donc non filtré par IP — la propriété qui manquait aux sources mortes
d'août) ; `200` = catalogue public.

| Fournisseur | Sonde | Catalogue constaté | Lanes | Budget/j | `terms_flag` |
|---|---|---|---|---|---|
| **groq** | (en prod) | compound-mini + llama 70b/8b | filter, analyze, settlement, search_read | 400 | — |
| **openrouter** | **200** | **421 modèles, 18 en `:free`** | analyze, filter, translate_cjk | 150 | — |
| **sambanova** | **200** | 7 (Llama-3.3-70B, DeepSeek-V3.2, gpt-oss-120b) | filter, analyze, settlement | 200 | — |
| **ovh** (UE) | **200** | 24 (Llama-3.3-70B, Qwen3-32B, Mistral) | filter, analyze, translate_cjk | 150 | — |
| **scaleway** (UE) | **401** | derrière clé | filter, analyze | 150 | — |
| **ollama_cloud** | **200** | 19 (GLM-5.2, Kimi K3, gpt-oss, DeepSeek-V4) | settlement, analyze, translate_cjk | 100 | — |
| **nvidia_nim** | **200** | 102 (DeepSeek-V4-Flash, Llama) | analyze, filter | 200 | ⚖️ `evaluation` |
| **cohere** | **401** | derrière clé | analyze | 30 | ⚖️ `non_commercial` |
| **zhipu** (Z.ai) | **401** | derrière clé (GLM-4.7-Flash) | translate_cjk, filter | 200 | ⚖️ `non_commercial` |
| **modelscope** | **200** | **46, dont toute la gamme Qwen3 + GLM-4.7-Flash** | translate_cjk, analyze | 150 | — |
| **siliconflow** | **401** | derrière clé | translate_cjk | 100 | — |
| **upstage** | **401** | derrière clé | translate_cjk | 50 | ⚖️ `evaluation` |

**8 fournisseurs sur 12 sont utilisables en production** (`PRODUCTION_SAFE`) ; les 4 marqués ne sont
jamais choisis par `route()` sauf `allow_flagged=True` explicite.

`daily_requests` est un budget **prudent côté PREDATOR**, pas la limite du fournisseur : on veut
basculer *avant* de se faire couper, jamais après — leçon du compte api-sports trouvé **suspendu**
le 2026-08-20.

### Recherche / lecture web (renforts de Tavily)

| Service | Sonde | Note |
|---|---|---|
| Jina Reader (`r.jina.ai`) | **200** | répond **même sans clé** ; palier gratuit généreux |
| Jina Search (`s.jina.ai`) | **401** | clé requise |
| Serper | **403** | clé requise (POST) — 2 500 requêtes offertes |
| SerpAPI | **200** | 100 recherches/mois |

### Non enrôlés, et pourquoi

- **endpoints anonymes sans clé** (Pollinations, LLM7…) : exactement le défaut fatal des sources
  sans clé de l'incident du 10→20 août — filtrés par IP depuis les runners, CGU floues. La leçon
  est acquise, on ne la repaie pas. Verrouillé par un test (`test_les_endpoints_anonymes_ne_sont_pas_enroles`) ;
- **multi-comptes d'un même fournisseur** : CGU. Verrouillé par `test_un_seul_compte_par_fournisseur` ;
- **carte requise pour un essai expirant** (Cerebras, Fireworks) : sauf décision opérateur ;
- **Mistral** : reste **hors registre**, c'est le domaine de panne isolé de Wiz. Verrouillé par
  `test_wiz_nest_pas_servi_par_le_routeur`.

## 3. Les lanes — la symbiose

Chaque appel IA **déclare son besoin**, pas son fournisseur. C'est ce qui permet de remplacer un
fournisseur mort sans toucher à un seul call site.

| Lane | Ordre de préférence (registre) | Déclarée par |
|---|---|---|
| `FILTER` | groq → sambanova → ovh → scaleway | `harvester.fetch_estimated_prices` (tier light) |
| `ANALYZE` | groq → openrouter → nvidia_nim → ovh → ollama_cloud | défaut du tier heavy |
| `TRANSLATE_CJK` | zhipu → modelscope → openrouter → ovh → siliconflow | `team_aliases.resolve_with_ai` |
| `SEARCH_READ` | groq (compound-mini) → Tavily → Jina → Serper | `oracle`, `harvester` |
| `SETTLEMENT` 🔒 | groq → sambanova → ollama_cloud (batch de nuit) | `settlement.fetch_match_result` |
| `WIZ` | Mistral seul — **hors routeur**, inchangé | `core/wiz_*` |

### La réserve de settlement est gardée EN NÉGATIF

Le 2026-08-02, le scan a épuisé le TPD Groq et le settlement n'a plus rien réglé de la journée :
ledger vide, `/performance` figé. La parade n'est pas de « réserver » des jetons au settlement —
c'est d'**amputer les autres lanes** :

```python
if lane != SETTLEMENT and SETTLEMENT in p.lanes:
    ceiling = max(0, ceiling - SETTLEMENT_RESERVE)   # 80 req/j
```

Personne ne « prend » la réserve : les autres lanes s'arrêtent avant et n'y ont **jamais** accès.
Vérifié par `test_la_lane_settlement_garde_son_budget_quand_le_scan_a_tout_pris` : à budget scan
épuisé, `lane_providers(FILTER) == []` pendant que `lane_providers(SETTLEMENT) == ["groq"]`.

### Règles transverses

- **Cache d'abord, toujours.** Le cache 30 min de la mission 2 s'applique AVANT tout appel, quel que
  soit le fournisseur — souvent plus rentable que de la capacité en plus.
- **Jamais de double dépense.** Un prompt auquel le premier fournisseur a répondu quelque chose de
  valide n'est jamais rejoué ailleurs (`test_un_prompt_valide_nest_jamais_rejoue_ailleurs`).
- **Une réponse inutilisable est une panne.** JSON invalide ou réponse vide comptent comme échec de
  disjoncteur : du point de vue du pipeline, un fournisseur qui consomme le quota sans rien produire
  est en panne — et c'est la panne la plus coûteuse.
- **Disjoncteur par fournisseur** : 3 échecs consécutifs → 30 min de repos, état partagé dans `meta`.

## 4. Preuve de bascule

### En live, contre le vrai catalogue OpenRouter

```
catalogue OpenRouter récupéré : 421 modèles
le modèle codé en dur dans le repo est-il présent ? False
WARNING ai_router[openrouter_demo]: modèle préféré
  'meta-llama/llama-3.3-70b-instruct:free' absent du catalogue
  — bascule sur 'nvidia/nemotron-3-super-120b-a12b:free'
=> modèle retenu : nvidia/nemotron-3-super-120b-a12b:free
=> bascule détectée : True

--- cas où AUCUNE préférence ne survit ---
ERROR ai_router[tout_mort]: AUCUNE préférence au catalogue — fournisseur écarté ce run
=> (None, True)
```

### En test (`tests/test_ai_router.py::TestBasculeDeModele`)

| Cas | Attendu |
|---|---|
| préférence de tête vivante | `("bon", False)` — aucune bascule |
| préférence de tête **morte** | `("secours", True)` — bascule **loggée** |
| **aucune** préférence au catalogue | `(None, True)` — fournisseur écarté du run |
| catalogue **illisible** | `("prefere", False)` — on ne débranche pas un fournisseur parce que son `/models` est momentanément muet |

Ce dernier cas est un choix : un ensemble vide veut dire « je ne sais pas », **pas** « aucun
modèle ». Confondre les deux couperait un fournisseur sain sur un hoquet réseau.

## 5. Alerte de lane — et le piège du bruit

`refresh_catalogues()` tourne au **démarrage de chaque run** (`run_engine._refresh_ai_catalogues`)
et alerte sur Telegram quand une lane tombe sous **2 fournisseurs sains**.

**Sauf quand aucun fournisseur n'est configuré du tout.** Ce cas a été trouvé par un test existant
(`test_reprice_mode.py::test_reprice_empty_cache_exits_quietly`, qui garde « un tick muet ne spamme
pas Telegram ») : la première version alertait sur les 5 lanes à chaque run en mode REPRICE — mode
qui n'utilise aucune IA. Zéro fournisseur n'est pas une dégradation, c'est un choix de déploiement,
et il est déjà visible. **On n'alerte que sur une capacité qui EXISTAIT et se dégrade** — sinon on
fabrique le bruit qui fait qu'on n'ouvre plus les alertes, donc qu'on rate la vraie.

La lane `WIZ` n'alerte jamais non plus : elle est mono-fournisseur **par construction**.

## 6. Section « santé IA » du rapport hebdo

`scripts/weekly_report.py` gagne `format_ai_health()` (pure, testable) :

```
🤖 *Santé IA* — tokens/jour, échecs, bascules
✅ *groq* — 312/400 appels · 84210 tokens · 0 échec(s) consécutif(s) · 0 bascule(s)
⚠️ *openrouter* — 44/150 appels · 12030 tokens · 1 échec(s) consécutif(s) · 4 bascule(s)
🔴 repos *zhipu* ⚖️non_commercial — 6/200 appels · 900 tokens · 3 échec(s) consécutif(s) · 0 bascule(s)
   → ⚠️ bascules répétées : openrouter — palier gratuit probablement en train de se refermer
```

Le compteur qui compte est **le nombre de bascules**. Une bascule isolée, c'est le routeur qui fait
son travail. Des bascules répétées sur le même fournisseur annoncent un palier gratuit en train de
se refermer — et c'est ça qu'on veut voir venir, plutôt que de découvrir un matin que le repli ne
repliait plus rien depuis des semaines.

## 7. Tests ajoutés (44)

| Fichier | Tests | Ce qui est verrouillé |
|---|---|---|
| `tests/test_ai_router.py` | 34 | Bascule de modèle (les 4 cas) · alerte de lane et **non-alerte quand rien n'est configuré** · disjoncteur · réponse vide/invalide = panne · pas de double dépense · **réserve de settlement** · fournisseurs morts absents du registre · un compte par fournisseur · endpoints anonymes non enrôlés · Wiz hors routeur · cache avant tout · chaque clé du registre documentée dans `.env.example` |
| `tests/test_ai_providers.py` | 10 (réécrits) | Délégation d'`ai_search` au routeur · quota compté · routeur en panne ne remonte jamais d'exception · le modèle OpenRouter mort n'est plus une préférence |

## 8. Ce qui reste à l'opérateur

1. **Ouvrir UN compte chez au moins deux fournisseurs** de la section 4bis de `.env.example` — une
   lane sous 2 fournisseurs sains alerte à chaque run. Les plus rentables en premier :
   **OpenRouter** (la plus grande variété par une seule clé), **SambaNova**, **OVH** (UE).
2. **Pour la lane CJK** (mission 3) : **ModelScope** ou **Z.ai** — un modèle chinois résout un nom
   d'équipe CJK bien mieux qu'un Llama généraliste, et le volume est minuscule grâce au dictionnaire
   `team_aliases`.
3. **Décider pour les fournisseurs marqués** (`nvidia_nim`, `cohere`, `zhipu`, `upstage`) : ils sont
   enrôlés mais exclus de la production tant que l'opérateur ne tranche pas leur compatibilité
   d'usage. Rien ne les active par accident.
4. **Vérifier SambaNova** : le palier ~200k tokens/j par modèle était documenté sans carte ; la
   sonde publique ne permet pas de confirmer si une carte est désormais exigée.
