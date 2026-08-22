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
   OddsAPI      (sharp, pool de clés)     Registre de 17 fournisseurs
   Matchbook    (sharp, sans clé)         Lanes : FILTER / ANALYZE /
   api-sports   (soft)                            TRANSLATE_CJK /
   odds.500.com (gratuit, mode ombre)             SEARCH_READ /
   7M           (noms d'équipes)                  SETTLEMENT / WIZ
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
   └──────────┬───────────┘        │  wiz_analysis, app_secrets)    │
              │                     └───────────────┬────────────────┘
              ▼                                     │
     Telegram (alertes)                             ▼
                                    ┌────────────────────────────────┐
   core/audit_engine.py ───────────►│  api/index.py — dashboard Flask│
   core/settlement.py               │  (Vercel, LECTURE SEULE)       │
   core/closing_line.py  (CLV)      └────────────────────────────────┘
   core/learning_layer.py (seuils appris, verdicts par sport)

   run_wiz.py + core/wiz_* — analyse contextuelle (Mistral, HORS registre
   IA : domaine de panne séparé). N'écrit QUE dans wiz_analysis.
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
| `/wiz` | Analyse contextuelle par match (`wiz_analysis`), avec ses sources |

Pas de build, pas de bundler : Jinja + CSS + JavaScript inline. Le dashboard
n'écrit qu'une chose, une demande de scan dans `meta` (`/api/scan`, cooldown
de 120 s), ramassée par `golden_hour.yml` au passage suivant.

### 🧠 Couche IA

- **Routeur multi-fournisseurs** ([`core/ai_router.py`](core/ai_router.py)) —
  registre de 17 fournisseurs, dont 9 utilisables en production (les autres
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
- **Wiz** (Mistral) est délibérément **hors** de ce registre : un domaine de
  panne séparé, pour qu'un incident sur le routeur ne prenne pas Wiz avec lui.

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
| `ODDS_API_KEYS` / `ODDS_API_KEY` | Pool de clés The-Odds API (rotation auto ; source de vérité : `app_secrets`) | ✅ |
| `SUPABASE_URL` / `SUPABASE_KEY` / `SUPABASE_SERVICE_KEY` | Supabase (anon pour lire, service_role pour écrire) | ✅ |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | Notifications Telegram | ✅ |
| `GROQ_API_KEY` / `TAVILY_API_KEY` | Recherche web (settlement + prix Pinnacle de repli) | ❌ |
| `API_SPORTS_KEY` / `ODDS_API_IO_KEY` | Books soft authentifiés par clé (Tier 2) | ❌ |
| `MISTRAL_API_KEY` | Wiz — raisonnement **et** recherche web (domaine de panne séparé) | ❌ |
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
5. **Mistral** (Wiz, optionnel) — [console.mistral.ai](https://console.mistral.ai/)

---

## 📡 API Endpoints

### Pages (Dashboard Flask)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Dashboard — signaux actifs |
| `/wiz` | GET | **Wiz v10.0** — analyse contextuelle IA, classée par `wiz_rank_score` (lecture seule de `wiz_analysis`) |
| `/ledger` | GET | Bilan CLV par sport |
| `/audit` | GET | Audit CLV détaillé + seuils dynamiques |
| `/performance` | GET | Rapport de performance AI learning (`ai_learning_ledger`) |

### API

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/signals` | GET | Signaux actifs **encore jouables** — coup d'envoi non passé (`?all=1` pour la liste brute, diagnostic) |
| `/api/wiz` | GET | Analyses Wiz + drapeau `enforce` (JSON) |
| `/api/scan` | POST | Demander un scan PAIM — pose le flag `meta.scan_request`, ramassé par `golden_hour.yml` (≤ 30 min) |
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

Les chemins normaux restent le cron de `audit.yml` (toutes les 6 h) et le
`workflow_dispatch` depuis l'interface GitHub ; cette route n'est qu'un
raccourci d'opérateur.

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
│   ├── ai_router.py         # Registre 17 fournisseurs, lanes, disjoncteur, budgets
│   ├── ai_search.py         # Recherche web (délègue au routeur) + pool de clés Groq
│   ├── daily_quota.py       # Comptage des budgets journaliers
│   │  ── Wiz (domaine de panne SÉPARÉ) ───────────────────────────────
│   ├── wiz_ai.py            # Client Mistral — volontairement hors du registre
│   ├── wiz_engine.py        # Prompts, parsing, pondération des tiers, wiz_rank_score
│   ├── wiz_sources.py       # Sources d'actualité (Google News + Bing News RSS)
│   │  ── Infrastructure ──────────────────────────────────────────────
│   ├── db.py                # Source unique des clients Supabase (lecture vs écriture)
│   └── secret_store.py      # Table `app_secrets` — BAT os.environ
├── templates/               # index / wiz / ledger / audit / performance / system
├── sql/                     # Migrations Supabase — À APPLIQUER À LA MAIN
├── tests/                   # pytest — voir AUDIT.md pour la carte des invariants testés
├── .github/workflows/       # 14 workflows — tout le calcul tourne ici
├── run_engine.py            # Scan complet : collecte, purge, émission
├── run_audit.py             # Settlement + CLV
├── run_wiz.py               # Wiz — batch d'analyse (n'écrit que wiz_analysis)
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
├── .python-version          # 3.11 — doit rester aligné avec vercel.json et les workflows
└── vercel.json              # Déploiement Vercel (importe api.index:app)
```

Les automatisations (scans, audit, rapport, backfill) sont pilotées par les workflows GitHub Actions dans [`.github/workflows/`](.github/workflows/), pas par les fichiers `main.py`/`config.py` d'une ancienne version de ce README.

---

## 🎯 Système 7/9

Le cœur du système PAIM :

1. **9 signaux** sélectionnés par cycle (8h)
2. **7 wins minimum** requis pour profitabilité
3. **Win rate historique** : 90.2%
4. **ROI mensuel moyen** : +100%

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
- **Python 3.11** — version unique du dépôt (`.python-version`, `vercel.json`,
  les 14 workflows). Aucun build.
- **Flask + Jinja** — dashboard, servi en serverless sur Vercel
- **Supabase** (PostgreSQL) — seul état persistant
- **Routeur IA maison** ([`core/ai_router.py`](core/ai_router.py)) — 17
  fournisseurs enregistrés, 9 utilisables en production ; aucun modèle codé
  en dur. Mistral pour Wiz, hors registre.

### Frontend
- **Aucun framework, aucun bundler** — Jinja, CSS écrit à la main
  ([`api/static/css/predator.css`](api/static/css/predator.css)), JavaScript
  inline. Pas de `node_modules`, pas d'étape de build.
- **Tailwind via CDN** — uniquement sur `/system`, qui est écrite en JSX
  compilé dans le navigateur. Les cinq autres pages n'en dépendent pas.

> « Chart.js — Graphiques financiers » figurait ici : le dépôt ne contient
> aucune bibliothèque de graphiques. « BetterStack — Log monitoring » aussi :
> les variables `BETTERSTACK_*` ne sont lues par aucun fichier (elles sont
> conservées dans `.env.example` avec une mention *UNUSED* explicite).

### DevOps
- **GitHub Actions** — 14 workflows ; tout le calcul y tourne
- **Vercel** — déploiement du dashboard sur push vers `main`
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