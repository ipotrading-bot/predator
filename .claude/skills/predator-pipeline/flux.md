# Flux de données — détail

> Référence de la skill `predator-pipeline`. Sections déplacées verbatim
> depuis SKILL.md (découpage du 2026-09-02), rien n'a été résumé.

## Data flow (in order)

1. **Odds ingestion** — `core/odds_api.py` (Tier 1, real Pinnacle+1XBet via The Odds
   API) → `core/harvester.py` (Tier 2 : LineFeed + api-sports + odds-api.io +
   titan007, enrichi par l'exchange Matchbook). **La recherche web a été
   SUPPRIMÉE le 2026-09-02 avec Groq/Tavily** (avant elle, Gemini l'avait été
   le 2026-07-21) : plus d'oracle LLM, plus de `fetch_pinnacle_prices`, plus
   d'estimateur Tier 3 — un match sans prix sharp RÉEL est écarté. Si un
   diagnostic vous ramène à Groq/Tavily/oracle, c'est cette skill qui était
   périmée, pas le code.
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
   Pass 1: `core/settlement.py` (`settle_signal`, score réel 100 % DÉTERMINISTE :
   api-sports puis `core/score_sources.py` — MLB statsapi, TheSportsDB par
   équipe ; ZÉRO appel IA depuis le 2026-09-02) → `status='settled'`.
   Pass 2 fallback: CLV depuis la closing line DÉJÀ capturée par les scans
   (colonne `closing_pinnacle_price`, sources oddsapi/exchange) →
   `status='closed'`, sinon `'expired'` (proxy).
   Every successful path inserts one row into `ai_learning_ledger`.
5. **Learning layer** — `core/learning_layer.py` `compute_and_save()`, called at the
   end of `audit_engine.run()`. Reads last 50 `ai_learning_ledger` rows per sport,
   needs ≥10 samples with `outcome not in ('expired', None)` before it will move a
   threshold. Thresholds persist to Supabase `meta` as `threshold_<sport>` and are
   read back by `run_engine.py` (`_load_thresholds`) as the next scan's `min_edge`.
6. **L'ancien sous-système d'analyse IA par match — SUPPRIMÉ le 2026-08-26**
(règle dure n°8 de `CLAUDE.md`, qui le nomme). Sa page, son moteur, son
workflow, ses tests et sa lane du routeur n'existent plus (décision
opérateur : « cette page ne me sert pas »). Ne pas les rechercher, ne pas les
recréer. Sa table d'analyses a été SUPPRIMÉE de la base le 2026-08-26 (DROP
définitif, migration `v10_6` appliquée — aucune archive n'existe).
Conséquence à connaître pour tout diagnostic IA : **Mistral n'est plus hors
registre**, c'est un fournisseur ordinaire de `core/ai_router.py` sur les
lanes `filter`/`analyze` (recherche de signaux). Il n'a jamais été validé par
une inférence réelle — `python scripts/ops.py ai` est le seul juge.


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
| 2 | **odds-api.io** (`core/odds_api_io.py`) | soft (1X2 + totals + handicaps) | `ODDS_API_IO_KEY` + pool `ODDS_API_IO_KEYS` | 500/jour et 2 books PAR COMPTE |
| 2 | **Titan007** (`core/titan007.py`) | soft **et** sharp, foot | aucune | ~500/jour (tolérance) |
| 2bis | LineFeed 1xbet/Melbet | soft | aucune | bloqué par IP |
| ~~3~~ | ~~recherche web~~ | — | — | SUPPRIMÉE le 2026-09-02 (Groq/Tavily retirés du pipeline) |

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
le tri sharp du Tier 2, donc chaque match servi par l'exchange est un match
de plus qui garde son edge calculable au lieu de partir en « Échec prix
Sharp ». Supprimer l'un des deux appels rétablit silencieusement la panne
du 2026-08-20.

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
