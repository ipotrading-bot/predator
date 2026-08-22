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
