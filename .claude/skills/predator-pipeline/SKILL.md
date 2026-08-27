---
name: predator-pipeline
description: Reference map of the PREDATOR PAIM data pipeline (odds ingestion → signal engine → Supabase → audit → learning layer → dashboard) and its known cross-file invariants. Use this BEFORE diagnosing "why is X empty/wrong/not updating" anywhere in this repo (run_engine.py, core/*, api/index.py, templates/*), before touching purge/audit/learning-layer logic, and before adding a new sport or Supabase column — it names the exact files that must stay in sync and the manual steps this stack does NOT automate.
---

# PREDATOR pipeline map

This project has no automated migration runner, and its test suite (`tests/`,
run by `ci.yml` on every push, which now gates the Vercel deploy) covers the maths and parsing logic but cannot
see live data or cron behaviour — to catch a *pipeline* break you still have to
trace the flow by hand. This skill is that trace, pre-done.

## Data flow (in order)

1. **Odds ingestion** — `core/odds_api.py` (Tier 1, real Pinnacle+1XBet via The Odds
   API) → `core/harvester.py` (Tier 2/3, recherche web + MMA/eSports/alt sports) →
   `core/oracle.py` (repli match par match, max 3 appels/scan). **Gemini a été
   SUPPRIMÉ du repo le 2026-07-21 (commit 0a7332e)** : toute la recherche passe
   par `core/ai_search.py` (Groq + Tavily). Si un diagnostic vous ramène à
   Gemini, c'est cette skill qui était périmée, pas le code.
2. **Signal generation** — `run_engine.py` `run()` calls `_process_h2h` /
   `_process_totals` / `_process_spreads`, which call into `core/math_engine.py`
   (devigging: `calc_dnb`, `devig_prob`, `to_binary`) and `core/paim_engine.py`
   (`compute_alpha`, `calculate_consensus_price`, `strict_team_match`). Output rows
   are quota-balanced by `_portfolio_balance` and written to Supabase `signals`
   (`status='active'`).
3. **Purge** — `_purge_old_signals()` runs at the TOP of every `run_engine.py`
   invocation (Golden Hour: hourly since 2026-07-23, was every 30 min). It must only ever delete rows scoped to
   `status='active'` for anything keyed on `match_time`/lifecycle. Never add an
   unscoped `.lt("match_time", ...)` or `.lt("created_at", ...)` rule without an
   explicit `.eq("status", "active")` — history for `settled`/`closed`/`expired`
   rows is retained here on purpose so `core/audit_engine.py` (which runs on a much
   slower 6h cadence) has time to reach them. A single unscoped purge rule
   previously deleted signals the instant kickoff passed, silently starving
   `ai_learning_ledger` and the `/performance` page for months — check this file
   first if either looks empty again.
4. **Audit** — `run_audit.py` → `core/audit_engine.py` (`run()`, cron: every 6h).
   Pass 1: `core/settlement.py` (`settle_signal`, score réel via `core/ai_search.py` — Groq/Tavily) →
   `status='settled'`. Pass 2 fallback: CLV vs current Pinnacle line via
   `core/oracle.py` → `status='closed'` (real closing line) or `'expired'` (proxy).
   Every successful path inserts one row into `ai_learning_ledger`.
5. **Learning layer** — `core/learning_layer.py` `compute_and_save()`, called at the
   end of `audit_engine.run()`. Reads last 50 `ai_learning_ledger` rows per sport,
   needs ≥10 samples with `outcome not in ('expired', None)` before it will move a
   threshold. Thresholds persist to Supabase `meta` as `threshold_<sport>` and are
   read back by `run_engine.py` (`_load_thresholds`) as the next scan's `min_edge`.
6. **Wiz — SUPPRIMÉ le 2026-08-26.** La page `/wiz`, son moteur
(`run_wiz.py`, `core/wiz_*`), son workflow `wiz.yml`, ses tests et la lane
`WIZ` du routeur n'existent plus (décision opérateur : « la page wiz ne me
sert pas »). Ne pas les rechercher, ne pas les recréer. La table
`wiz_analysis` a été SUPPRIMÉE de la base le 2026-08-26 (DROP définitif,
`sql/migrate_v10_6_drop_wiz.sql` appliquée — aucune archive n'existe).
Conséquence à connaître pour tout diagnostic IA : **Mistral n'est plus hors
registre**, c'est un fournisseur ordinaire de `core/ai_router.py` sur les
lanes `filter`/`analyze` (recherche de signaux). Il n'a jamais été validé par
une inférence réelle — `python scripts/ops.py ai` est le seul juge.

## The sport-key invariant

These four places must list the exact same sport keys, or a sport silently gets
scanned but never learned-from (or vice versa):
- `core/odds_api.py` `SPORT_KEYS` (what's actually fetched) — the ground truth.
- `core/constants.py` `KELLY_FRACTION`.
- `core/learning_layer.py` `SPORT_DEFAULTS`.
- `run_engine.py` `SPORT_QUOTA` / `_SPORT_ORDER` (portfolio balancer).

`api/index.py`'s `_SPORT_EMOJI`/`_SPORT_LABEL`/`_DEFAULT_T` dicts (used by
`/ledger`) intentionally list a *superset* of display-only sports (tennis, mma,
darts, cricket, etc.) that are not currently harvested — that's harmless UI cruft,
not a bug, unless one of those keys starts appearing in real `signals` rows.

## Incident 10→20 août 2026 : dix jours à « 0 matchs, 0 signaux » — et ce qui l'empêche désormais

Toutes les sources sont mortes la même journée : clé OddsAPI à 0 crédit (401
`OUT_OF_USAGE_CREDITS`, jamais tournée), LineFeed 1xbet/Melbet en timeout
depuis les runners GitHub, Tavily au plafond mensuel (HTTP 432), les 3 clés
Groq à 100k TPD — brûlées par les ~40 runs/jour qui retentaient le harvest
web à vide — et API-Football muet (un /odds par fixture ≠ 200 → `continue`
sans log). Quatre mécanismes v10.3 (commit « fix: key pool… ») :

- **Pool de clés OddsAPI** — `core/odds_api.py::candidate_keys()` lit
  `ODDS_API_KEYS` (plusieurs clés) puis `ODDS_API_KEY` (app_secrets puis env),
  sonde chaque clé gratuitement (`/v4/sports`), et sur 401/403/422 en cours de
  scan bascule sur la suivante EN REJOUANT LA MÊME LIGUE. Seul un pool
  entièrement mort rend `[]`. Ajouter : `python scripts/rotate_odds_key.py
  --add <clé>` ; état : `--show`. Tests : `tests/test_odds_api_keypool.py`.
- **Alertes Telegram avec la cause, dédupliquées 6h** — `_alert_oddsapi_pool_if_dead()`
  nomme « N/N clés épuisées — rotation requise » ; l'ancien « Melbet
  inaccessible » partait 40×/jour sans jamais le dire. Horodatages dans `meta`
  (`alert_*`).
- **Coupe-circuit harvest** — `meta.harvest_empty_at` : un Tier 2 vide n'est
  pas retenté avant `HARVEST_EMPTY_TTL_H` (3h). Préserve le TPD Groq pour le
  settlement (qui en a besoin pour les scores). Tests :
  `tests/test_engine_circuit_breaker.py`.
- **API-Football utile** — `/odds` PAR DATE paginé (≤ 7 req/cycle au lieu de
  50+) et **Pinnacle extrait de la réponse** (`odds_pinnacle`) → signaux foot
  sans recherche web ; run_engine honore un `odds_pinnacle` déjà présent et
  ne l'envoie pas à `fetch_pinnacle_prices`. Une ligne de log « API-Football: »
  par cycle, succès ou échec. Tests : `tests/test_api_football.py`.

## Les sources de cotes et la règle qui les départage (2026-08-20)

Le moteur a besoin de DEUX côtés : un prix SHARP (probabilité vraie par
dévigorisation) et un prix SOFT (le prix réellement jouable). Une source qui
n'apporte qu'un côté ne produit aucun signal seule.

**La règle empirique, mesurée depuis une IP datacenter :** tout ce qui
s'atteint SANS clé est filtré par IP, donc mort depuis les runners GitHub.
LineFeed 1xbet/Melbet/22bet (203/404), ESPN (403 Akamai), SofaScore (403),
Oddspedia (challenge Cloudflare). N'y consacrez plus de temps : les sources
qui marchent sont celles qui authentifient par CLÉ (la clé sert de
laissez-passer là où l'IP est refusée) — ou Matchbook, qui ne filtre pas.

| Étage | Source | Côté | Clé | Quota |
|---|---|---|---|---|
| 1 | The Odds API (`core/odds_api.py`) | sharp + soft | pool `ODDS_API_KEYS` | 500/mois par clé |
| 1.5 | **Matchbook** (`core/matchbook.py`) | **sharp** (1X2 + totals + handicaps) | aucune | 700 req/min |
| 1.5 | Betfair (`core/harvester.py`) | sharp | 499 £ + géobloqué US | inopérant sur Actions |
| 2 | **api-sports** (`core/api_sports.py`) | soft **et** sharp (Pinnacle) | `API_SPORTS_KEY` | 100/jour PAR sport |
| 2 | **odds-api.io** (`core/odds_api_io.py`) | soft (1X2 + totals + handicaps) | `ODDS_API_IO_KEY` | 500/jour |
| 2 | **Titan007** (`core/titan007.py`) | soft **et** sharp, foot | aucune | ~500/jour (tolérance) |
| 2bis | LineFeed 1xbet/Melbet | soft | aucune | bloqué par IP |
| 3 | recherche web (`core/ai_search.py`) | sharp estimé | Groq/Tavily | quotas morts régulièrement |

`python scripts/ops.py sources` sonde tout cela sans dépenser un crédit —
c'est la commande à lancer AVANT tout diagnostic « pourquoi 0 signal ».

**Le couple qui rend le pipeline autonome.** Matchbook (sharp, sans clé) +
odds-api.io (soft, 1xbet) donnent les DEUX côtés d'un edge sans aucune clé
payante et sans recherche web — sur 1X2, totals ET handicaps. C'est ce qui
lève la dépendance structurelle à OddsAPI. Deux points de vigilance :
- les deux sources choisissent leur ligne principale avec la MÊME heuristique
  (celle dont les deux prix sont les plus proches) ; les faire diverger
  ferait comparer deux lignes différentes. Le garde `LINESKIP` de
  `_process_totals`/`_process_spreads` (écart > 0,5) est le filet ;
- un match trouvé dans l'autre sens passe par `_flip_exchange_prices()` :
  le handicap porte le signe de l'équipe, donc il s'inverse aussi. L'oublier
  produit un edge calculé contre la mauvaise ligne, sans rien casser.

**Betfair Exchange n'est branchable nulle part** (vérifié 2026-08-20) : son
API propre demande 499 £ ET refuse les IP américaines ; chez odds-api.io les
books sharp/exchanges sont réservés aux plans payants (HTTP 403 explicite).
Matchbook tient ce rôle — ne pas repayer cette recherche.

**api-sports — les paramètres réels, vérifiés le 2026-08-20.** La première
implémentation les avait devinés d'après la doc, et les quatre sports
répondaient « refus applicatif » :
- le calendrier n'accepte que `date=` (un appel par journée). `from`/`to`
  exigent `league`+`season` côté foot et **n'existent pas** sur les trois
  autres hôtes. Une fenêtre de 24h chevauche deux dates : il faut donc DEUX
  appels, sinon la moitié tardive du slate manque ;
- les cotes du FOOT s'obtiennent par `date=` + `page=` (plusieurs matchs par
  réponse) — c'est ce qui rend la source tenable dans 100 requêtes/jour ;
- les cotes des autres sports s'obtiennent UNIQUEMENT par `game=<id>`, une
  requête par match, d'où `MAX_GAME_ODDS`. `league`+`season` y est fermé au
  plan gratuit au-delà de 2022.
Rendement mesuré avec un compte neuf : foot 30 matchs pour 8 requêtes, tous
avec un prix Pinnacle ; basket 7 pour 10 requêtes.

**Titan007 — pourquoi elle est là, et ce qu'elle exige.** Matchbook est riche
en Amérique du Sud et en ligues secondaires ; odds-api.io ne l'est pas. Le
2026-08-20, ce décalage ne laissait que 8 matchs des deux côtés. Titan007
couvre exactement ce gisement (286 matchs/jour, 85 ligues, jusqu'à 157 books
dont Pinnacle) et apporte le soft ET le sharp d'un coup : **28 matchs des
deux côtés** après branchement. Trois choses à ne pas défaire :
- **le fuseau est UTC+8 et n'est écrit nulle part.** Calibré contre les heures
  UTC de Matchbook sur 14 matchs communs (12 concordances exactes). Huit
  heures d'erreur, et chaque signal est refusé par le garde « match déjà
  commencé » — ou réglé sur le mauvais match ;
- **le prix soft est borné par la médiane** (`MAX_SOFT_OUTLIER`). Sur 157
  books, prendre le maximum ramasse le book figé : un match colombien
  ressortait à 4,59 contre 3,58 sharp, un edge de 28 % entièrement faux ;
- **les URL ne doivent JAMAIS porter de query string.** Le robots.txt de
  `bf.titan007.com` interdit `/*?*` ; les deux endpoints utilisés en sont
  exempts. Le feed de handicap asiatique en porte une : il est volontairement
  absent. C'est une source TOLÉRÉE, pas contractuelle — cadence basse,
  budget journalier, et retour [] en cas de panne.

**Le piège qui a tenu le pipeline à zéro malgré deux sources saines.** Les
fournisseurs ne nomment pas les matchs pareil : « Cde Juventud Italiana »
contre « Club Juventud Italiana », « CSD Macara » contre « Deportivo
Macara ». Mesuré le 2026-08-20 sur 13 matchs odds-api.io contre 53 marchés
Matchbook, le rapprochement par clé EXACTE en appariait **0**, le flou
**8**. `run_engine._lookup_exchange()` essaie donc, dans l'ordre : clé
exacte, clé exacte inversée, puis `strict_team_match`. Deux règles y sont
verrouillées par `tests/test_engine_circuit_breaker.py` :
- en flou, un candidat UNIQUE seulement — deux prétendants, on renonce
  (poser le mauvais prix sharp donne un edge faux et parfaitement muet) ;
- noms de moins de 3 caractères ignorés, et lignes sans `home`/`away` aussi :
  `strict_team_match` renvoie `True` dès qu'un nom est VIDE, donc sans ce
  garde une ligne incomplète s'apparierait à tout le slate.

**L'enrichissement par l'exchange tourne DEUX fois par scan.** Le Tier 1.5
s'exécute avant le Tier 2 ; les matchs d'odds-api.io/api-sports n'existent
pas encore à ce moment-là. Le second appel a lieu juste avant
`fetch_pinnacle_prices()`, donc chaque match servi par l'exchange est un
match de moins à chercher sur le web (quota Groq préservé pour le
settlement). Supprimer l'un des deux appels rétablit silencieusement la
panne du 2026-08-20.

**Matchbook — ce qu'il faut savoir avant d'y toucher.** Le milieu back/lay
donne une marge d'environ 0,1 % (meilleure que Pinnacle, ~2 %), d'où son rôle
de référence sharp. Trois pièges encodés dans `core/matchbook.py` et gardés
par `tests/test_matchbook.py` : (1) « A **at** B » signifie que B reçoit —
l'inverse de « A vs B » ; confondre les deux intervertit les cotes 1 et 2 en
silence ; (2) le foot expose `one_x_two`, les sports US/tennis `money_line` ;
(3) un carnet vide affiche back 110 / lay 1.01 — sans le garde-fou de
fourchette et de somme des probabilités, ces prix entreraient dans le devig.
Testé OK depuis une IP **GB** ; le géoblocage US n'a PAS pu être vérifié —
chercher « Matchbook » dans les logs Actions, et `MATCHBOOK_OFF=1` coupe la
source si besoin.

Si « 0 signaux » revient : chercher d'abord dans les logs « OddsAPI clé #…
écartée », « harvest SAUTÉ » et « API-Football: » — les trois disent la cause
en clair.

## The OddsAPI quota reality (2026-07-23)

**RÉSOLU depuis le 2026-08-04 — ne plus diagnostiquer ainsi.** Il y avait
autrefois DEUX clés `ODDS_API_KEY` distinctes (une sur Vercel pour
`/api/odds-quota`, une dans les secrets GitHub pour le moteur), et le dashboard
pouvait afficher un rassurant 500 pendant que le moteur était à sec. La table
`app_secrets` (Supabase) est désormais la source unique : `core/secret_store.py`
la lit AVANT `os.environ`, et le 2026-08-06 il a été vérifié en direct que
Vercel la lit bien (rotation de clé → `/api/odds-quota` passé à 500/0 sans
redeploy). Le widget et le moteur voient donc la même clé.

Deux corollaires qui restent vrais :
- une valeur PÉRIMÉE dans `app_secrets` bat un `os.environ` correct ;
- ~~le secret GitHub `ODDS_API_KEY` doit rester NON VIDE~~ **PÉRIMÉ.** Cette
  garde échouait FERMÉ : elle aurait tué tous les scans le jour du retrait du
  secret. Elle a été supprimée avec l'obsolescence d'OddsAPI (2026-08-26), et
  le préflight actuel (`scripts/ci_env.py`) ne l'exige plus — c'est même un
  test explicite
  (`tests/test_ci_env.py::test_preflight_odds_api_key_nest_plus_requise`).

Les logs de scan (`x-requests-used` / `x-requests-remaining`) restent la mesure
la plus fiable de la consommation réelle.

**PÉRIMÉ — ce garde n'existe plus.** Supprimé le 2026-08-01 sur décision
opérateur « ne pas rationner » ; le scan ne s'arrête plus que sur un vrai 422.
Conservé ici parce que la SIGNATURE décrite reste utile à reconnaître dans
d'anciens logs. Ce qui subsiste est le pré-vol GRATUIT `_events_in_window()`
(endpoints `/v4/sports` et `/events`, 0 crédit) qui évite de payer une ligue
vide. Conséquence historique du garde disparu :
below 50, it trips after the FIRST sport key of every scan, so the engine
silently falls back to harvester/cache/Betfair for everything. The counter
then looks frozen (47 across five consecutive runs) because that single
request isn't billed. A frozen quota number is the signature of this state,
not of a healthy one.

Ordre de grandeur (à jour 2026-08-06) : le plan fait 500 req/MOIS. `SPORT_KEYS`
compte **18** clés depuis le retrait de la Coupe du Monde. Le coût se paie par
LIGUE PEUPLÉE et non par match : mesuré via `/events` en fenêtre 24h, un scan
coûte **14 crédits** (5 ligues peuplées ce jour-là), et 0 en fenêtre 2h. À 12
scans engine + 4 deep par jour, cela fait ~224 crédits/jour, soit **une clé
tous les ~2 jours**. La rotation passe par `app_secrets` (Supabase), pas par le
secret GitHub ni par Vercel — voir `core/secret_store.py`.

## Le recentrage sports du 2026-08-22 (mission « recentrage / quota / apprentissage »)

Détail complet : `reports/refonte_scope_2026-08.md`. Ce qu'il faut savoir pour diagnostiquer :
- **Retirés** : eSports, tennis de table, volleyball, handball (`RETIRED_SPORTS`,
  `core/constants.py`). Le garde vit dans `_emit` : aucun signal possible, même
  depuis un cache meta résiduel ou un slate REPRICE. Les fonctions de recherche
  web `fetch_esports_events`/`fetch_alternative_sports_batch`/`fetch_mma_events`
  N'EXISTENT PLUS. Lignes historiques conservées, settlement inchangé.
- **Plus aucun sport pricé par recherche web** : MMA et boxe (h2h) via
  `mma_mixed_martial_arts`/`boxing_boxing`, NFL (`americanfootball_nfl`, gardée
  par `SEASON_OPENS` — pas de présaison), LdC/UEL, Euroleague (sport-type
  `euroleague_basketball`, mécaniques basketball, Kelly 0.12). Le pré-vol rend 0
  hors saison/hors carte : l'ajout ne coûte rien.
- **L'invariant des 4 fichiers est désormais testé** (`tests/test_new_sports_phase2.py`,
  `tests/test_retired_sports.py`) : tout sport-type de `SPORT_KEYS` doit être
  dans `KELLY_FRACTION`, `SPORT_DEFAULTS`, `_QUOTA_FAST/_QUOTA_DEEP`, `SPORT_EMOJI`.
- **Politique de dépense OddsAPI** (`core/scan_windows.py`, injectée dans
  `fetch_odds`) : en fenêtre favorable → payé ; sport avec un signal actif à
  < 240 min du coup d'envoi → payé (closing line prioritaire) ; sinon 180 min mini
  entre deux scans payants d'une ligue, et sous `ODDS_API_RESERVE_CREDITS` (60) le
  fond s'espace. Chercher « DÉPENSE | » dans les logs pour savoir POURQUOI une
  ligue peuplée n'a pas été payée. `pool_remaining()` suit `x-requests-remaining`.
- **Verdicts par sport** (`meta.sport_verdict_<sport>`, posés par
  `compute_and_save` à chaque audit) : `promotion_eligible` (≥30 réglés, Wilson bas
  > rentabilité) / `perte_prouvee` / `non_demontre` → « retrait proposé ». Jamais
  appliqués : `KELLY_FRACTION` ne bouge que par commit.

## Le mode REPRICE (2026-08-22) — l'odds screen gratuit

`REPRICE=1` (step accroché au tick `golden` de `scan.yml`, chaque heure) relit le slate soft
photographié par les scans complets dans `meta.cache_soft_slate` (TTL 4h,
`CACHE_SOFT_SLATE_TTL_H`) et le recompare à un prix sharp **Matchbook
frais** — gratuit, sans clé, 700 req/min. Émission NON fantôme. Invariants
verrouillés par `tests/test_reprice_mode.py` :
- AUCUNE source payante ni recherche web : OddsAPI, api-sports, odds-api.io,
  titan007, blocs MMA/eSports/alt, `fetch_pinnacle_prices` — jamais appelés ;
  le coupe-circuit `harvest_empty_at` n'est ni lu ni écrit ; un cache vide
  sort en silence (heartbeat, zéro alerte). L'absence des clés payantes dans
  l'env du step est la garantie mécanique.
- Le slate est écrit par les scans COMPLETS uniquement (ni golden hour —
  fenêtre 2h partielle — ni reprice, sinon TTL immortel), trimé par
  `_trim_soft_slate` : le sharp d'exchange et les prix `_estimated` n'y sont
  JAMAIS sérialisés (l'exchange doit repricer frais à chaque tick).
- `_save` fait depuis le même jour select-then-update-or-insert scopé
  `status='active'` : id et `created_at` stables, colonnes de clôture
  (`clv_pct_real`…) préservées sous re-scan horaire — l'ancien
  delete-then-insert les détruisait. `run_rapport.py` filtre sur
  `created_at` (pas `scanned_at`, rajeuni chaque heure par REPRICE).
- Dédup Telegram : `_dedup_systems_for_telegram` (clé meta
  `alert_system_<hash du contenu>`, TTL 6h) — le même combo ne repart pas à
  chaque tick ; un tick REPRICE sans rien de neuf se tait.
- Le CLV réel est désormais un critère de PREMIER rang de la couche
  d'apprentissage (`_decide_threshold` : montée si avg_clv < −1% sur ≥15
  lignes, descente accélérée si > +1% avec majorité positive) et
  `_archive_before_purge` passe par `log_to_ledger` (les expirés portent
  enfin `clv_pct_real`/`kelly_pct`/`sharp_prob`).
- Limite assumée : le prix soft peut avoir jusqu'à 4h — le mouvement détecté
  est côté sharp ; l'advice donne la cote à vérifier au book.

## L'arbitrage de cadence (2026-08-22)

Quand le pool OddsAPI est mort, CHAQUE scan paie sur les budgets journaliers
des sources gratuites : api-sports 80 req/sport (~8 scans), odds-api.io 400
(~14), titan007 500 (~12, 41 req/scan). À 12 engine + 12 guerrilla + 4 deep,
le budget entier partait avant 08:30 UTC — dernier signal du 2026-08-21 émis
à 08:24, soirée européenne à sec. Cadences réduites à 8+2+2 = 12 scans
finançables. Golden hour ne paie rien (il sort avant le Tier 2 sans OddsAPI).
Ne pas remonter une cadence sans refaire ce tableau. Titan007 est branché
dans le chemin économique du coupe-circuit depuis le même jour (même classe
de bug que a0767c8 : source saine court-circuitée par ricochet).

## La refonte mathématique du 2026-08-22 — edge = EV vraie

Jusqu'au 2026-08-22, `compute_alpha` rendait un RATIO DE PRIX
(xbet/pinnacle − 1) contre une cote sharp encore VIGORISÉE, et `devig_prob`
appliquait une formule qui n'était ni la méthode puissance (que sa docstring
annonçait) ni la proportionnelle — favori sous-estimé de 2,6-3,1 points sous
1,10. Audit du ledger réglé : Brier de sharp_prob PIRE que la proba implicite
brute du book soft, pente de recalibration 0,12, CLV réel +0,18 % (t=0,18,
nul). 37 signaux sur 91 étaient partis avec kelly_pct=0 (refusés par la
couche de mise, publiés quand même). Depuis :
- `core/math_engine.devig()` = ensemble {proportionnelle, puissance, Shin},
  médiane renormalisée ; `devig_bounds()` rend aussi la borne worst-case
  (min des trois) — gardés par `tests/test_ev_gates.py` ;
- `edge_pct` = (prob dévigorisée × cote soft − 1) — une EV, plus un ratio.
  Les seuils/plafonds appris d'avant cette date (meta `threshold_*`,
  `edge_ceiling_*`, `odds_ceiling_*`) ont été SUPPRIMÉS le 2026-08-22 : ils
  étaient calibrés sur l'ancienne grandeur. Toute analyse du ledger qui
  traverse cette date compare deux échelles d'`initial_edge` différentes ;
- `_emit` refuse : EV worst-case ≤ 0, mise Kelly nulle (via
  `core/tax_engine.optimal_stake_fraction`, plus jamais la formule inline),
  et tout EV sous `EV_EDGE_FLOOR` (1,5 %, `core/constants.py`) — plancher
  appliqué DANS `_emit`, la règle AH0 ne peut pas le contourner.

## Manual steps this stack does NOT automate

- **Supabase schema changes** live in `sql/migrate_vX_Y.sql` but nothing runs them
  automatically — they must be pasted into the Supabase SQL Editor by a human with
  DB access. Check `sql/` for the latest unapplied migration before assuming a
  column exists.
- **`backfill_ledger.py`** (workflow: `.github/workflows/tools.yml`, input
  `backfill_ledger`) is
  `workflow_dispatch`-only, idempotent, one-shot. It re-populates
  `ai_learning_ledger` from historical terminal-status `signals` rows. Needed after
  any period where step 3's purge bug (or similar) silently dropped rows.
- Both of the above require credentials/permissions this agent does not have by
  default in a fresh sandbox (no Supabase URL/key, and the sandbox's `GITHUB_TOKEN`
  is an app-installation token without `workflow` scope — `gh workflow run` and
  `gh api .../dispatches` both 403). Don't assume a prior session's access
  persists; re-check with `env | grep -i supabase` and `gh auth status` rather than
  telling the user "done" on faith.

## Cron cadence (GitHub Actions, `.github/workflows/`)

| Workflow (mode) | Cadence | Purpose |
|---|---|---|
| `scan.yml` — `golden` | horaire (H+25), 24/j | scan de mouvement de ligne à T-120min, purge à chaque run, lit `meta.scan_request`. **Ses signaux partent en FANTÔME depuis le 2026-08-06** (`SHADOW_GOLDEN_HOUR`) : persistés et réglés, jamais recommandés — 39% de réussite pour 54,5% requis, p=0,007. Porte aussi le step **REPRICE** (section dédiée) — gratuit, non fantôme, avec un pool de secrets qui ne contient aucune clé payante. Ne PAS ajouter de poller dédié pour compenser la latence du bouton Scan — c'est l'erreur du 2026-07-07. |
| `scan.yml` — `standard` | **8x/jour sur les FENÊTRES FAVORABLES** (02/06/09/12/17/19/21/23 UTC) depuis le 2026-08-22 (était 12x/2h uniforme) | scan complet, fenêtre **24h**. Placement = `core/scan_windows.py` ; cadence dimensionnée sur le budget des sources gratuites — voir « L'arbitrage de cadence » |
| `scan.yml` — `deep` | **2x/jour (05:33, 17:33)** depuis le 2026-08-22 (était 4) | **fenêtre 24h elle aussi** — `HOURS_AHEAD: "24"` explicite. Ce qui reste « deep » = `MAX_MATCHES=100` et `_QUOTA_DEEP`, pas l'horizon. |
| `scan.yml` — `guerrilla` | **2x/jour (09:47, 21:47)** depuis le 2026-08-22 (était toutes les 2h) | scan sans OddsAPI (sources gratuites + recherche web renforcée, horizon 48h venu du CODE et non d'une variable) — c'est lui, pas un bouton, qui consomme le TPD Groq quand les sources sont mortes ; le coupe-circuit `harvest_empty_at` le neutralise 3h après un Tier 2 vide |
| `scan.yml` — passe closing line | **à la fin de chaque tick** (36/j) | `run_closing_line.py`, `continue-on-error` : une passe ratée n'annule pas le scan déjà persisté |
| `audit.yml` | toutes les 6h | settlement + CLV + couche d'apprentissage. **Ne pas renommer ce fichier** : `api/index.py` le déclenche par son nom. |
| `closing_line.yml` | **3 ticks/h (H+14/34/54)** depuis le 2026-08-26 (était `4-59/10`, 144/j) | capture de la ligne de clôture, cadence alignée sur `CLOSING_LINE_REFRESH_MIN`. **Hors du verrou d'écriture** — voir CLAUDE.md pour la justification exacte, la version courte (« aucune ligne en commun ») étant fausse. |
| `reports.yml` — `rapport` | **toutes les 2h (H+35)** | rapport Telegram. `run_rapport.py:REPORT_WINDOW_H` (2h) doit rester égal à l'intervalle du cron, sinon un même signal repart dans plusieurs rapports. |
| `reports.yml` — `hebdo` | **lundi 07:00 UTC** | classement des sports + `calibration_report.py` + **rapport hebdo de vérité** (`scripts/weekly_report.py` : CLV réel, Brier, ROI net taxe, SUSPECT, verdicts promotion/retrait) |
| `tools.yml` | manuel uniquement | `monte_carlo` et `backfill_ledger` (réparation one-shot de `ai_learning_ledger`) |
| `ci.yml` | sur push/PR | tests + lint, **puis** déploiement Vercel — le gate n'est réel que parce que `vercel.json` désactive le déploiement Git |

Total : **124 déclenchements planifiés/jour** (contre 196 avant le 2026-08-26).

**Le scheduler GitHub sous-livre ces crons** (mesuré 2026-08-27 : closing line
~5 %, trou de 9 h sur scan.yml). Un chien de garde Cloudflare
(`scripts/cloudflare_watchdog_worker.js`, cron `*/10`, déployé par
`scripts/deploy_watchdog_worker.py`) dispatche un rattrapage `workflow_dispatch`
quand un workflow est en retard sur sa cadence — récit et règles dans
INCIDENTS.md (« Le scheduler GitHub ne livre qu'une fraction des crons »),
invariants gardés par `tests/test_watchdog_worker.py`. Avant de diagnostiquer
une cadence : les runs `workflow_dispatch` dans `gh run list` peuvent être des
rattrapages du chien de garde, pas des clics d'opérateur.
Le mode d'un tick de `scan.yml` est déduit du cron qui a tiré
(`scripts/ci_scan_mode.py::CRON_MODES`) : un cron ajouté sans sa ligne fait
échouer le run ET le test.

When a fix touches purge, audit, or learning-layer logic, sanity-check it against
this cadence table — anything that runs more often than `audit.yml` (6h) can race
ahead of settlement if it isn't carefully scoped to `status='active'`.

**2026-07-07 incident**: `on_demand.yml` used to poll `meta.scan_request` on its
own `*/5 * * * *` schedule (288 triggers/day, ~81% of every scheduled trigger in
the repo combined). GitHub Actions silently delays/drops scheduled runs under
that kind of load — le tick golden (alors `golden_hour.yml`), despite being declared `*/30`, was
actually landing 1–4.5h apart, leaving the dashboard's "Dernier scan" hours
stale. Fix: the schedule was removed from `on_demand.yml`, and its
`meta.scan_request` check was folded into a step at the top of the golden
scan (free — it rides its existing cadence instead of its own separate
schedule ; depuis le 2026-08-26 ce step est `scripts/ci_scan_mode.py`, lu à
chacun des 36 ticks de `scan.yml`). `on_demand.yml` itself was deleted
outright on 2026-07-07 — once the golden scan absorbed the check, the file
was pure dead weight (a `workflow_dispatch`-only duplicate of logic that now
lives in the scan workflow) and it additionally never passed
`SUPABASE_SERVICE_KEY` to `run_engine.py`, so any manual trigger of it was
guaranteed to fail every write via RLS regardless of secret correctness. When
`scan_request` is pending, golden_hour runs `run_engine.py` with
`GUERRILLA=1` instead of `GOLDEN_HOUR=1` for that tick and clears the flag
(using `SUPABASE_SERVICE_KEY` for the DELETE — the anon key can't write
`meta` either, see [[project_predator_supabase]]). If dashboard "Scan" button
latency or scan cadence looks off again, check this step first before
re-adding a dedicated poller — a new dedicated schedule is exactly the
mistake that caused the original throttling.

## La couche d'apprentissage — deux pièges corrigés le 2026-08-06

`core/learning_layer.py` fixe les planchers d'edge (`meta.threshold_<sport>`)
que le prochain scan lira. Deux défauts s'y combinaient et ont fini par étouffer
l'émission (1 signal/jour début août contre 22 le 2 août) :

1. **Le critère était absolu** — monter à <60% de réussite, ne descendre qu'à
   >82%. Un pari à cote 1,85 est rentable dès 54,1% et aucun segment du ledger
   n'a jamais atteint 82% : la montée se déclenchait toujours, la descente
   jamais. Cliquet jusqu'au plafond dur de 6,0%, puis silence. La règle est
   désormais ancrée sur `p_breakeven` (cote moyenne + `TAX_RATE`), et reste
   asymétrique : monter ne demande pas de preuve, descendre exige que la borne
   basse de Wilson passe la rentabilité.
2. **Il apprenait sur des paris qu'on ne joue plus** — `playable_rows()` filtre
   maintenant sur la zone 2-24h avant le coup d'envoi. Hors zone : 113 paris,
   ROI -28,5%, p=0,002, le seul segment significatif du ledger — et il n'est
   plus jouable (>24h hors fenêtre de scan, <2h en fantôme). Le football était
   jugé sur 50,0% quand sa zone jouable fait 65,1%.

**Le piège d'analyse à ne pas refaire.** Pris marginalement, les totals
(-19,2%), les edges ≥10% (-25,3%) et les cotes ≥2,00 (-24,7%) semblent être les
coupables. C'est faux : ces trois ensembles se recoupent (33 paris communs) et
sont concentrés HORS zone jouable. À l'intérieur, ce sont les MEILLEURS
segments — totals +27,2%, edge ≥10% +10,5%, cote >1,95 +17,8% — et une
régression logistique contrôlant la cote ne laisse aucun d'eux significatif.
Les couper ferait tomber le ROI de la zone de +9,4% à +3,4%. **Toujours
conditionner sur la zone jouable avant de conclure quoi que ce soit sur un
sport, un marché ou une bande d'edge.**
