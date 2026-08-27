# 🦅 PREDATOR PAIM — pipeline de paris sportifs à edge quantitatif

> Ingestion de cotes → moteur de signaux (devig / edge) → Supabase →
> settlement & CLV → couche d'apprentissage → dashboard Flask en lecture seule.
>
> Tout le calcul tourne dans des crons GitHub Actions. Le dashboard n'écrit
> rien (à une exception documentée : la demande de scan).

> **Numéro de version** : il n'y en a plus qu'un, `DASHBOARD_VERSION` dans
> [`api/index.py`](api/index.py), rendu par `/api/health` et par les six pieds
> de page. Cet en-tête a porté « v8.8 » longtemps après que le dashboard soit
> passé en 10.x — un numéro recopié à la main n'est mis à jour par personne.

---

## 🏗 Architecture

```
   SOURCES DE COTES                       COUCHE IA (core/ai_router.py)
   ───────────────────                    ──────────────────────────────
   OddsAPI      (OBSOLÈTE, cf. plus bas)  Registre de 18 fournisseurs
   Matchbook    (sharp, sans clé)         Lanes : FILTER / ANALYZE /
   api-sports   (soft)                            TRANSLATE_CJK /
   odds.500.com (gratuit, mode ombre)             SEARCH_READ /
   7M           (noms d'équipes)                  SETTLEMENT
   Kalshi / Polymarket (consensus)        Disjoncteur : 3 échecs → 30 min
          │                               Découverte du catalogue au run
          ▼                                        │
   ┌──────────────────────┐                        │
   │  core/harvester.py   │◄───────────────────────┘
   │  collecte + appariement (par temps + ligue + STRUCTURE de cotes)
   └──────────┬───────────┘
              ▼
   ┌──────────────────────────────────────────────┐
   │  core/math_engine.py + core/paim_engine.py   │  devig, edge, consensus
   │  core/risk_manager.py + core/tax_engine.py   │  Kelly, garde-fous
   └──────────┬───────────────────────────────────┘
              ▼
   ┌──────────────────────┐        ┌────────────────────────────────┐
   │  run_engine.py       │───────►│  Supabase  (signals, meta,     │
   │  scan, purge, émission│        │  ai_learning_ledger,          │
   └──────────┬───────────┘        │  app_secrets)                  │
              │                     └───────────────┬────────────────┘
              ▼                                     │
     Telegram (alertes)                             ▼
                                    ┌────────────────────────────────┐
   core/audit_engine.py ───────────►│  api/index.py — dashboard Flask│
   core/settlement.py               │  (Vercel, LECTURE SEULE)       │
   core/closing_line.py  (CLV)      └────────────────────────────────┘
   core/learning_layer.py (seuils appris, verdicts par sport)

```

Détail des invariants inter-fichiers et des pièges connus :
[`CLAUDE.md`](CLAUDE.md) et [`AUDIT.md`](AUDIT.md).

---

## ✨ Ce que le système fait réellement

Cette section a été **réécrite le 2026-08-22 après vérification poste par
poste contre le code**. Elle annonçait auparavant une console de log « style
Matrix », des courbes Chart.js, une intégration QuantStats, un export PDF, un
ticker de news, des ratios Sortino/Calmar et un monitoring BetterStack :
*aucun* de ces sept éléments n'existe dans le dépôt, et certains n'ont jamais
existé. Un README qui décrit un autre produit fait chercher les bugs au
mauvais endroit.

### 📊 Dashboard (6 pages, lecture seule)

| Page | Contenu réel |
|---|---|
| `/` | Signaux actifs **encore jouables**, groupés par sport, filtres et tri client |
| `/ledger` | Bilan CLV par sport, seuils d'edge appris |
| `/audit` | Distribution d'alpha par sport, verdicts de promotion/retrait |
| `/performance` | WIN/LOSS/PUSH depuis `ai_learning_ledger`, score de Brier |
| `/system` | Calculateur de paris système, pré-rempli avec les signaux actifs |

Pas de build, pas de bundler : Jinja + CSS + JavaScript inline. Le dashboard
n'écrit qu'une chose, une demande de scan dans `meta` (`/api/scan`, cooldown
de 120 s), ramassée par `scan.yml` au tick suivant (36/jour, donc ≤ ~1 h).

### 🧠 Couche IA

- **Routeur multi-fournisseurs** ([`core/ai_router.py`](core/ai_router.py)) —
  registre de 18 fournisseurs, dont 10 utilisables en production (les autres
  portent un `terms_flag` : usage non commercial, évaluation, ou palier
  gratuit fermé). Aucun nom de modèle n'est codé en dur : chaque lane déclare
  une liste de préférences et le routeur retient le premier modèle qui existe
  vraiment dans le catalogue publié au moment du run.
- **Répartition sur 24 h** — les fournisseurs sont triés par budget
  *restant*, pas par ordre du registre : sans cela le premier était drainé
  pendant que les autres restaient intacts (mesuré : 240 appels tous sur
  Groq → 42 après correction).
- **Réserve settlement gardée en négatif** — les autres lanes sont amputées
  et n'y touchent jamais. Le règlement des paris passe avant tout le reste.
- **Mistral** y est entré le 2026-08-26, avec la suppression de Wiz : il en
  était le fournisseur exclusif et vivait hors registre à ce titre. Son quota
  sert désormais les lanes de signaux (`filter`, `analyze`).

### 📈 Mesure de la performance

- **CLV** (closing line value) — le juge de paix : battre la ligne de clôture
  ou ne pas parier.
- **Score de Brier** — calibration des probabilités émises.
- **Seuils d'edge auto-ajustés par sport**, et verdicts de promotion/retrait
  (≥ 30 paris réglés, Wilson vs rentabilité). Ils sont **loggés, jamais
  appliqués automatiquement** : la décision reste humaine.
- **Kelly fractionné** et garde-fous de risque ([`core/risk_manager.py`](core/risk_manager.py)).

### 🔔 Notifications

- **Telegram** — signaux émis, alertes de quota (paliers 20 % et 5 %,
  dédupliquées 24 h), alerte quand une lane IA tombe sous deux fournisseurs
  sains.

---

## 🚀 Installation

### 1. Cloner le repository

```bash
git clone https://github.com/ipotrading-bot/predator.git
cd predator
```

### 2. Installer les dépendances

```bash
pip install -r requirements.txt
```

### 3. Configurer l'environnement

```bash
cp .env.example .env
# Éditer .env avec vos clés API
```

### 4. Lancer le serveur Flask

```bash
python api/index.py
# Ou avec Gunicorn pour production:
# gunicorn api.index:app
```

### 5. Accéder au Dashboard

```
http://localhost:5000
```

---

## 🔑 Configuration

### Variables d'Environnement Requises

| Variable | Description | Requis |
|----------|-------------|--------|
| `ODDS_API_KEYS` / `ODDS_API_KEY` | Pool de clés The-Odds API. **OBSOLÈTE depuis le 2026-08-26** : `ODDS_API_ENABLED` vaut 0, le Tier 1 ne s'exécute plus. Réactivation par `ODDS_API=1` | ❌ |
| `SUPABASE_URL` / `SUPABASE_KEY` / `SUPABASE_SERVICE_KEY` | Supabase (anon pour lire, service_role pour écrire) | ✅ |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | Notifications Telegram | ✅ |
| `GROQ_API_KEY` / `TAVILY_API_KEY` | Recherche web (settlement + prix Pinnacle de repli) | ❌ |
| `API_SPORTS_KEY` / `ODDS_API_IO_KEY` | Books soft authentifiés par clé (Tier 2) | ❌ |
| `MISTRAL_API_KEY` | Fournisseur du registre IA — lanes `filter` / `analyze` | ❌ |
| `NFL_SEASON_START` | Date d'ouverture NFL (défaut `2026-09-10`) — pas de scan de présaison | ❌ |
| `ODDS_API_RESERVE_CREDITS` / `BACKGROUND_MIN_INTERVAL_MIN` | Politique de dépense OddsAPI (`core/scan_windows.py`) — défauts 60 / 180 | ❌ |

Le périmètre des sports (retraits du 2026-08-22, MMA/boxe/NFL/LdC/UEL/Euroleague sur flux OddsAPI réel,
budget crédits avant/après, carte des crons, boucle de calibration) est documenté dans
[`reports/refonte_scope_2026-08.md`](reports/refonte_scope_2026-08.md).

### Obtenir les Clés API

1. **The-Odds API** — [the-odds-api.com](https://the-odds-api.com/) (500 crédits/mois/clé — prévoir un pool de 3-4 clés)
2. **Groq** — [console.groq.com](https://console.groq.com/) · **Tavily** — [tavily.com](https://tavily.com/)
3. **api-sports** — [api-sports.io](https://api-sports.io/) · **odds-api.io** — [odds-api.io](https://odds-api.io/)
4. **Supabase** — [supabase.com](https://supabase.com/) (Plan gratuit)
5. **Mistral** (optionnel) — [console.mistral.ai](https://console.mistral.ai/)

---

## 📡 API Endpoints

### Pages (Dashboard Flask)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Dashboard — signaux actifs |
| `/ledger` | GET | Bilan CLV par sport |
| `/audit` | GET | Audit CLV détaillé + seuils dynamiques |
| `/performance` | GET | Rapport de performance AI learning (`ai_learning_ledger`) |

### API

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/signals` | GET | Signaux actifs **encore jouables** — coup d'envoi non passé (`?all=1` pour la liste brute, diagnostic) |
| `/api/scan` | POST | Demander un scan PAIM — pose le flag `meta.scan_request`, ramassé par `scan.yml` à chaque tick (≤ ~1 h). **Limitée à 3 demandes / 5 min par IP** ; un jeton d'admin en dispense |
| `/api/audit/run` | POST | Déclencher `audit.yml` — **jeton d'admin requis** (`X-Predator-Token`), voir ci-dessous |
| `/api/health` | GET | Santé du **dashboard** (aucun appel à Supabase — reste utilisable base injoignable) |

Toutes les routes sont définies dans [`api/index.py`](api/index.py) — c'est la seule source de vérité, cette table doit rester synchronisée avec ce fichier.

#### Route d'administration — `DASHBOARD_ADMIN_TOKEN`

`/api/audit/run` était **ouverte à tout Internet** jusqu'au 2026-08-22 : un
POST anonyme déclenchait `audit.yml`, soit 45 minutes de runner, le settlement
et la consommation de la réserve IA — sans authentification, sans cooldown,
sans limite de débit, sur une URL Vercel publique. Aucune interface du dépôt
ne l'appelait ; elle n'était connue que de cette table.

Elle exige désormais un jeton, et elle **échoue fermé** : sans
`DASHBOARD_ADMIN_TOKEN` configuré sur le déploiement, elle refuse tout.

```bash
# Générer un jeton, puis le poser sur Vercel (Settings → Environment Variables)
python -c "import secrets; print(secrets.token_urlsafe(32))"

curl -X POST https://<déploiement>/api/audit/run \
     -H "X-Predator-Token: $DASHBOARD_ADMIN_TOKEN"
```

**Le jeton passe UNIQUEMENT par l'en-tête.** La forme `?token=…`, admise
jusqu'au 2026-08-27 « pour un curl d'opérateur », est désormais **refusée
même avec le bon jeton** : une URL est écrite en clair dans les logs d'accès
Vercel, ceux du proxy, l'historique du shell, l'en-tête `Referer` envoyé à
tout tiers et l'historique du navigateur — et ces journaux survivent au
jeton, une rotation ne les efface pas. Un `curl` qui l'utilisait encore
recevra un 401 ; le log du déploiement en donne la raison.

Les chemins normaux restent le cron de `audit.yml` (toutes les 6 h) et le
`workflow_dispatch` depuis l'interface GitHub ; cette route n'est qu'un
raccourci d'opérateur.

#### Le dashboard n'a plus de clé d'écriture — `SUPABASE_SERVICE_KEY` RETIRÉE

Depuis le 2026-08-27, `api/index.py` n'écrit plus rien. `/api/scan`, sa seule
écriture, passe par `demander_scan()` — une fonction Postgres `security
definer` appelable avec la clé de LECTURE, qui porte elle-même le cooldown et
la limite de débit (`sql/migrate_v10_9_scan_request_rpc.sql`).

Tant que `SUPABASE_SERVICE_KEY` restait posée sur Vercel, une faille de la
fonction publique donnait les pleins pouvoirs sur `signals`,
`ai_learning_ledger`, `meta` et `app_secrets`. Elle n'y a plus aucun usage.

**FAIT le 2026-08-27.** Le déploiement ne porte plus que ces QUATRE
variables — la liste exacte de ce que le code lit :

| Variable | Rôle |
|---|---|
| `SUPABASE_URL` | lecture |
| `SUPABASE_KEY` | clé anon, LECTURE SEULE — l'écriture passe par `demander_scan()` |
| `DASHBOARD_ADMIN_TOKEN` | `/api/audit/run` |
| `GITHUB_PAT` | `/api/audit/run` déclenche `audit.yml` |

Onze variables ont été retirées le même jour, dont `SUPABASE_SERVICE_KEY`.
Les autres n'avaient JAMAIS servi au dashboard : `GROQ_API_KEY`,
`GEMINI_API_KEY`, `ODDS_API_KEY`, `NEWS_API_KEY`, `TELEGRAM_BOT_TOKEN`,
`TELEGRAM_CHAT_ID`, `POSTGRES_URL`, `POSTGRES_PRISMA_URL`,
`POSTGRES_URL_NON_POOLING`, `POSTGRES_PASSWORD`. Le dashboard n'appelle ni IA,
ni source de cotes, ni Telegram, ni Postgres en direct — vérifié sur le graphe
d'import réel : il ne charge que `core.constants`, `core.db`, `core.perf_view`
et `core.stats_utils`. Un secret posé là où il ne sert pas n'est pas une
commodité, c'est de l'exposition.

Ces clés restent intactes là où elles servent (secrets GitHub Actions pour le
pipeline, `app_secrets` pour les sources) : seule la copie Vercel a disparu.
`POSTGRES_*` était injecté par l'intégration Vercel↔Supabase et peut y être
réintroduit par une resynchronisation — à revérifier après toute manipulation
de l'intégration.

**Si l'opération est à refaire, l'ORDRE compte** : appliquer la migration,
DÉPLOYER le nouveau code, vérifier `/api/scan`, et seulement ensuite retirer
la clé. L'inverse casse la route entre les deux étapes. Un changement de
variable n'agit qu'au déploiement suivant —
`python scripts/ops.py vercel redeploy`.

Vérifié en production après retrait : `/api/health` à `db_configured: true`,
les cinq pages en 200, `POST /api/scan` → `queued` (donc la fonction Postgres
suffit, sans aucune clé d'écriture), `POST /api/audit/run` sans jeton → 401.

---

## 📁 Structure du Projet

```
predator/
├── api/
│   ├── index.py             # Flask — les 6 pages + l'API. DASHBOARD_VERSION y vit.
│   └── static/              # CSS écrit à la main, icônes PWA, manifest
├── core/
│   │  ── Sources de cotes ────────────────────────────────────────────
│   ├── odds_api.py          # Tier 1 — The Odds API (Pinnacle + 1XBet réel), POOL de clés
│   ├── odds_api_io.py       # Tier 2 — odds-api.io (books soft authentifiés)
│   ├── matchbook.py         # Sharp, sans clé
│   ├── api_sports.py        # Soft (API_FOOTBALL_KEY / API_SPORTS_KEY)
│   ├── harvester.py         # Orchestration de la collecte + appariement
│   ├── oracle.py            # Prix Pinnacle unitaire (repli, max 3 appels/scan)
│   ├── free_sources.py      # Sources gratuites — appelé EN DERNIER par harvester
│   ├── source_adapter.py    # Cadre commun : appariement temps+ligue+STRUCTURE, divergence en POINTS
│   ├── odds500.py           # odds.500.com — 30 books, démarre en MODE OMBRE
│   ├── sevenm.py            # 7M — source de NOMS anglais (pas de cotes)
│   ├── titan007.py          # Source de résultats
│   ├── prediction_markets.py# Kalshi / Polymarket — rôle consensus
│   ├── team_aliases.py      # Dictionnaire d'alias (clé = id numérique, jamais le libellé)
│   │  ── Moteur de signaux ───────────────────────────────────────────
│   ├── math_engine.py       # Devigging (Power method), calc_dnb, to_binary
│   ├── paim_engine.py       # compute_alpha, consensus, strict_team_match
│   ├── constants.py         # Source unique : seuils, KELLY_FRACTION, risk_flag, RETIRED_SPORTS
│   ├── scan_windows.py      # Fenêtres favorables (UTC) + politique de dépense OddsAPI
│   ├── risk_manager.py      # Exposition, drawdown, disjoncteurs (global et par sport)
│   ├── tax_engine.py        # Fiscalité sur le gain net
│   ├── monte_carlo.py       # Simulation de trajectoires de bankroll
│   │  ── Règlement & apprentissage ───────────────────────────────────
│   ├── settlement.py        # Résultat réel du match → WIN/LOSS/PUSH
│   ├── audit_engine.py      # Pipeline settlement + CLV (run_audit.py)
│   ├── closing_line.py      # Capture de la ligne de clôture → CLV
│   ├── learning_layer.py    # Seuils d'edge par sport + verdicts promotion/retrait
│   ├── stats_utils.py       # Brier, Wilson, bucketisation
│   ├── perf_view.py         # Filtrage des lignes de /performance
│   │  ── Couche IA ───────────────────────────────────────────────────
│   ├── ai_router.py         # Registre 18 fournisseurs, lanes, disjoncteur, budgets
│   ├── ai_search.py         # Recherche web (délègue au routeur) + pool de clés Groq
│   ├── daily_quota.py       # Comptage des budgets journaliers
│   │  ── Infrastructure ──────────────────────────────────────────────
│   ├── db.py                # Source unique des clients Supabase (lecture vs écriture)
│   └── secret_store.py      # Table `app_secrets` — BAT os.environ
├── templates/               # index / ledger / audit / performance / system
├── sql/                     # Migrations Supabase — À APPLIQUER À LA MAIN
├── tests/                   # pytest — voir AUDIT.md pour la carte des invariants testés
├── .github/workflows/       # 6 workflows — tout le calcul tourne ici
├── run_engine.py            # Scan complet : collecte, purge, émission
├── run_audit.py             # Settlement + CLV
├── run_closing_line.py      # Capture des lignes de clôture
├── run_rapport.py           # Rapport Telegram (toutes les 2 h)
├── run_monte_carlo.py       # Simulation
├── backfill_ledger.py       # Réparation ponctuelle d'ai_learning_ledger
├── validator.py             # Health-check manuel (volontairement non importé)
├── scripts/
│   ├── ops.py               # Piloter Supabase/Vercel/secrets (doctor, status, ai…)
│   ├── weekly_report.py     # Rapport hebdo de vérité (lundi 07:00 UTC)
│   ├── rotate_odds_key.py   # Gestion du pool de clés OddsAPI
│   └── …                    # rank_sports, calibration_report, edge_frequency_audit
├── requirements.txt
├── .python-version          # 3.12 — APPARTIENT À VERCEL, ne PAS aligner sur les workflows
└── vercel.json              # Déploiement Vercel (importe api.index:app)
```

Les automatisations (scans, audit, rapport, backfill) sont pilotées par les workflows GitHub Actions dans [`.github/workflows/`](.github/workflows/), pas par les fichiers `main.py`/`config.py` d'une ancienne version de ce README.

---

## 🎯 Résultats réels

> ⚠️ Cette section annonçait un « Système 7/9 » : *9 signaux par cycle de 8 h,
> 7 wins minimum, 90,2 % de réussite historique, +100 % de ROI mensuel*.
> **Aucun de ces quatre chiffres n'était vrai, et le mécanisme lui-même
> n'existe nulle part dans le code** — `7/9`, `MIN_WINS`, « 9 signaux » : zéro
> occurrence. Il n'y a ni cycle de 8 h, ni quota de 9 signaux, ni seuil de
> 7 wins. Le portefeuille est équilibré par quota de sport
> ([`core/paim_engine.py`](core/paim_engine.py)) et la mise par Kelly
> fractionnaire fiscalisé, pas par un compte de paris gagnants.

Mesuré en base le 2026-08-27 sur `ai_learning_ledger` (327 lignes, dont
**114 réglées** en WIN/LOSS — les 182 `expired` ne sont pas des résultats) :

| | annoncé | mesuré |
|---|---|---|
| Taux de réussite | 90,2 % | **56,1 %** (64 W / 50 L) |
| ROI | +100 % / mois | **−10,3 %**, pondéré Kelly et net de taxe |

Et le taux nu ne suffit pas à conclure, dans un sens comme dans l'autre :

- intervalle de Wilson à 95 % : **[47,0 % ; 64,9 %]** ;
- à la cote moyenne de 1,739, il faut **62,8 %** pour être rentable après la
  taxe de 20 % sur le gain net ;
- la borne basse (47,0 %) est sous le point mort : **le système n'est pas
  prouvé rentable**. Il n'est pas prouvé perdant non plus — 114 paris ne
  tranchent pas.

⚠️ Ces lignes viennent de l'ANCIEN moteur. Les phases A1 et A6 (2026-08-27)
ont corrigé le prix retenu (exécutable, non dévigorisé) et la ligne comparée
(même handicap des deux côtés) ; depuis, le football n'émet plus aucun signal
positif. Une recalibration demande des lignes réglées POSTÉRIEURES à ces
corrections — voir `CLAUDE.md`. Ne pas recalculer un seuil sur ce ledger : il
décrit une distribution que le moteur ne produit plus.

Reproduire ces chiffres : `python scripts/replay_ledger_executable.py`
(lecture seule).

### Kelly Criterion

### 💰 Dimensionnement des mises

Kelly **fractionnaire, et la fraction dépend du sport** — elle vit dans
`KELLY_FRACTION` ([`core/constants.py`](core/constants.py)) :

| Sport | Fraction | Pourquoi |
|---|---|---|
| basketball | 0.15 | NBA — le marché le plus sharp du monde |
| hockey | 0.13 | NHL — sharp, liquide, peu de bruit |
| soccer / baseball | 0.12 | volume élevé |
| rugbyleague / aussierules / mma | 0.10 | marchés fiables mais moins validés au ledger |
| boxing | 0.08 | marché mince, jamais validé dans le ledger |
| *(défaut)* | 0.12 | |

> ⚠️ Ce README a annoncé **« Kelly 25% »** jusqu'au 2026-08-22, avec la
> formule `Mise = Bankroll × (Edge / Odds) × 0.25`. C'était faux dans un
> rapport de 2 à 3 : aucun sport n'a jamais dépassé 0.15. Sur un système de
> mise, un chiffre de documentation faux ne fait pas perdre du temps, il fait
> perdre de l'argent. La source de vérité est `KELLY_FRACTION`, jamais ce
> tableau — qui n'en est qu'un reflet daté.

Bankroll de référence : `BANKROLL_REF = 150 €` (100 000 XOF).

---

## 🛡 Gestion des risques

Ce qui existe réellement dans [`core/risk_manager.py`](core/risk_manager.py) :

- **Plafond d'exposition** — `MAX_EXPOSURE_PCT = 0.15` : jamais plus de 15 %
  de la bankroll engagés simultanément. C'est ce plafond qui dimensionne
  naturellement les nouveaux signaux quand plusieurs paris sont en cours.
- **Disjoncteur sur drawdown glissant** — `DRAWDOWN_LIMIT_PCT = 0.25` sur les
  `DRAWDOWN_WINDOW_N = 20` derniers paris tranchés (WIN/LOSS). Au-delà,
  l'émission s'arrête.
- **Disjoncteur par sport** — le même mécanisme, isolé sport par sport : un
  sport qui dérape ne coupe pas les autres.
- **Reprise MANUELLE uniquement** — `resume_emission()` est le seul chemin de
  redémarrage. Un disjoncteur qui se réarme tout seul n'est pas un
  disjoncteur.
- **Verdicts de promotion/retrait par sport** — calculés par
  [`core/learning_layer.py`](core/learning_layer.py) (≥ 30 paris réglés,
  Wilson vs rentabilité), **loggés et jamais appliqués automatiquement**.

> Les rubriques « Max Drawdown 15 % (hard stop) » et « Stop Loss dynamique
> selon volatilité » ont été retirées le 2026-08-22 : aucune constante ni
> aucun code ne les implémentait.

### Mesure de la performance

Il n'y a pas de tableau de « valeurs cibles » dans ce dépôt, et celui qui
figurait ici (Win Rate > 65 %, Sharpe > 2.0, Sortino > 2.5, Profit Factor
> 2.0) n'était calculé nulle part — ni Sortino ni Profit Factor n'existent
dans le code, et `sharpe` n'apparaît que dans un commentaire. Ce qui est
réellement mesuré, et où :

| Mesure | Où | Rôle |
|---|---|---|
| **CLV** | `core/closing_line.py`, `/ledger`, `/audit` | le juge de paix : battre la ligne de clôture |
| **Score de Brier** | `core/stats_utils.py`, `/performance` | calibration des probabilités émises |
| **Drawdown glissant** | `core/risk_manager.py` | déclenche le disjoncteur |
| **WIN/LOSS/PUSH, ROI** | `ai_learning_ledger`, `/performance` | résultat réalisé |
| **Seuils d'edge appris** | `core/learning_layer.py`, `meta.threshold_<sport>` | ajustement par sport |

---

## 📊 Technologies

### Backend
- **Deux interpréteurs, subis et non choisis.** `.python-version` et
  `vercel.json` disent **3.12** ; les 6 workflows, l'action
  `.github/actions/setup` et le dev local disent **3.11**. Ce n'est pas une
  dérive à corriger : l'image de build Vercel n'embarque pas 3.11, et
  « aligner » `.python-version` sur 3.11 casse le déploiement et laisse la
  production sur le commit précédent — vécu le 2026-08-22. Le code doit
  rester compatible avec les deux. Gardé par
  `tests/test_workflow_secrets.py`. Aucun build.
- **Flask + Jinja** — dashboard, servi en serverless sur Vercel
- **Supabase** (PostgreSQL) — seul état persistant
- **Routeur IA maison** ([`core/ai_router.py`](core/ai_router.py)) — 18
  fournisseurs enregistrés, 10 utilisables en production ; aucun modèle codé
  en dur. Mistral est au registre depuis la suppression de Wiz (2026-08-26).

### Frontend
- **Aucun framework, aucun bundler** — Jinja, CSS écrit à la main
  ([`api/static/css/predator.css`](api/static/css/predator.css)), JavaScript
  inline. Pas de `node_modules`, pas d'étape de build.
- **Tailwind servi par le dépôt** — uniquement sur `/system`, écrite en JSX
  compilé dans le navigateur. Le bundle est vendorisé
  (`api/static/js/tailwind-3.4.17.min.js`) : `cdn.tailwindcss.com` ne
  publie aucun en-tête CORS, donc l'intégrité SRI y est impossible et la
  seule fermeture est de ne pas l'appeler. React, React-DOM et Babel
  restent distants mais épinglés, avec `integrity` ET `crossorigin`.
  Les quatre autres pages n'en dépendent pas.

> « Chart.js — Graphiques financiers » figurait ici : le dépôt ne contient
> aucune bibliothèque de graphiques. « BetterStack — Log monitoring » aussi :
> les variables `BETTERSTACK_*` ne sont lues par aucun fichier (elles sont
> conservées dans `.env.example` avec une mention *UNUSED* explicite).

### DevOps
- **GitHub Actions** — 6 workflows (`scan`, `audit`, `closing_line`,
  `reports`, `tools`, `ci`) ; tout le calcul y tourne. Les quatre anciens
  workflows de scan ont fusionné dans `scan.yml` le 2026-08-26 — le mode
  vient du cron qui a tiré ([`scripts/ci_scan_mode.py`](scripts/ci_scan_mode.py)).
- **Vercel** — le dashboard N'EST PAS déployé par le push lui-même : le
  déploiement Git est désactivé dans `vercel.json`
  (`git.deploymentEnabled.main: false`) et c'est le job `deploy` de `ci.yml`
  qui pousse, en CLI, APRÈS une suite verte. Sans cette désactivation le
  `needs: test` ne protégerait rien : chaque commit partait deux fois, par
  la voie Git et par la CLI, en course.
- **pytest** — la suite doit rester à 0 échec ; **pyflakes** propre

---

## 🤝 Contribution

1. Fork le repository
2. Créer une branche feature (`git checkout -b feature/amazing`)
3. Commit les changes (`git commit -m 'Add amazing feature'`)
4. Push la branche (`git push origin feature/amazing`)
5. Ouvrir un Pull Request

---

## 📄 License

MIT License — Voir [LICENSE](LICENSE) pour les détails.

---

## ⚠️ Disclaimer

> **Le trading sportif comporte des risques.** Les performances passées ne garantissent pas les résultats futurs. N'investissez que ce que vous pouvez vous permettre de perdre. Ce logiciel est fourni à titre éducatif uniquement.

---

## 📞 Contact

- **GitHub** : [ipotrading-bot/predator](https://github.com/ipotrading-bot/predator)
- **Documentation** : [plans/](plans/)

---

*Built with 🧠 by the PREDATOR Team — Dakar Hub*