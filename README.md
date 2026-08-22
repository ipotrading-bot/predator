# 🦅 PREDATOR PAIM v8.8 — Hedge Fund Sportif Autonome

> **PHD MIT ARCHITECTURE | DAKAR HUB**
> 
> Système de trading sportif alimenté par l'IA, utilisant l'asymétrie d'information pour générer des signaux EV+ (Expected Value Positive).

---

## 🌟 Vue d'Ensemble

PREDATOR PAIM est un bot de trading sportif institutionnel qui combine :

- **Analyse quantitative** des cotes (Pinnacle vs Soft Books)
- **Intelligence Artificielle** (Gemini 2.0 + Groq Llama 3)
- **Gestion de risque** de type hedge fund (Kelly Criterion, Sharpe Ratio)
- **Dashboard professionnel** en temps réel

---

## 🏗 Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        PREDATOR PAIM v2.0                        │
├─────────────────────────────────────────────────────────────────┤
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐             │
│  │  PINNACLE   │  │   1XBET     │  │   RSS       │             │
│  │  (Sharp)    │  │  (Soft)     │  │   Feeds     │             │
│  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘             │
│         │                │                │                     │
│         └────────────────┴────────────────┘                     │
│                          │                                      │
│                 ┌────────▼────────┐                            │
│                 │   PAIM Engine   │                            │
│                 │                 │                            │
│                 │  ┌───────────┐  │                            │
│                 │  │  GROQ     │  │  ← Filtrage ultra-rapide   │
│                 │  │ (Llama 3) │  │     (< 100ms)              │
│                 │  └───────────┘  │                            │
│                 │  ┌───────────┐  │                            │
│                 │  │  GEMINI   │  │  ← Analyse contextuelle    │
│                 │  │  2.0      │  │     profonde               │
│                 │  └───────────┘  │                            │
│                 └────────┬────────┘                            │
│                          │                                      │
│         ┌────────────────┴────────────────┐                    │
│         ▼                ▼                ▼                     │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐             │
│  │  SUPABASE   │  │  TELEGRAM   │  │  DASHBOARD  │             │
│  │  Database   │  │  Notifs     │  │  Flask      │             │
│  └─────────────┘  └─────────────┘  └─────────────┘             │
└─────────────────────────────────────────────────────────────────┘
```

---

## ✨ Fonctionnalités

### 📊 Dashboard Institutionnel
- **Console de Log Temps Réel** — Terminal style Matrix pour suivre l'algorithme PAIM
- **Courbe d'Équité Dynamique** — Chart.js avec Sharpe, Drawdown, VaR
- **Métriques de Risque Avancées** — Brier Score, Sortino, Calmar Ratio
- **API Health Status** — LED indicators pour Pinnacle, 1XBet, Gemini, Telegram
- **Current Exposure** — Montant total engagé en temps réel
- **Market Ticker** — News sportives en défilement continu

### 🧠 Intelligence Artificielle
- **Groq (Llama 3)** — Filtrage bayésien ultra-rapide (< 100ms)
- **Gemini 2.0 Flash** — Analyse contextuelle profonde
- **Analyse de News** — Sentiment analysis sur les news sportives

### 📈 Analytics Professionnels
- **QuantStats Integration** — Rapports style hedge fund
- **Export PDF** — Performance report téléchargeable
- **Brier Score** — Précision des prédictions IA
- **Sharpe/Sortino** — Rendement ajusté au risque

### 🔔 Notifications
- **Telegram Ghost** — Alerts discrètes
- **BetterStack Logs** — Monitoring centralisé des erreurs

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
│   ├── index.py          # Flask app — toutes les routes (pages + API)
│   └── static/           # CSS, icônes, manifest PWA
├── core/
│   ├── odds_api.py        # Tier 1 — The Odds API (Pinnacle + 1XBet réel)
│   ├── harvester.py        # Tier 2/3 — soft books (api-sports, odds-api.io, Titan007) + recherche web Pinnacle
│   ├── scan_windows.py     # Fenêtres favorables (UTC) + politique de dépense OddsAPI (Phase 3, 2026-08-22)
│   ├── oracle.py            # Prix Pinnacle unitaire (fallback, max 3 appels/scan)
│   ├── math_engine.py     # Devigging (Power method), calc_dnb, to_binary
│   ├── paim_engine.py      # compute_alpha, consensus, strict_team_match
│   ├── settlement.py        # Résultat réel du match → outcome WIN/LOSS/PUSH
│   ├── audit_engine.py     # Pipeline settlement + CLV (run_audit.py)
│   ├── learning_layer.py  # Seuils MIN_EDGE adaptatifs par sport + verdicts promotion/retrait (meta sport_verdict_*)
│   ├── wiz_ai.py             # Wiz — client Mistral (domaine de panne SÉPARÉ de Groq/Tavily)
│   ├── wiz_engine.py         # Wiz — prompts, parsing, pondération des tiers, wiz_rank_score
│   ├── constants.py         # Single source of truth (seuils, Kelly, risk_flag, seuils Wiz)
│   └── db.py                 # Single source of truth pour les clients Supabase (lecture vs écriture)
├── templates/               # index / wiz / ledger / audit / performance / system
├── sql/                      # Migrations Supabase (à exécuter manuellement)
├── tests/                    # pytest — core/math_engine, core/constants, core/db
├── run_engine.py            # Pipeline de scan complet (Portfolio Balancer inclus)
├── run_audit.py             # Entry point de core/audit_engine.py
├── run_rapport.py           # Rapport Telegram (toutes les 2h)
├── scripts/weekly_report.py # Rapport hebdo de vérité : CLV réel, Brier, ROI net taxe, SUSPECT, verdicts (lundi 07:00 UTC)
├── run_wiz.py               # Wiz — batch d'analyse contextuelle (cron 2h, n'écrit que wiz_analysis)
├── backfill_ledger.py       # Script one-shot de réparation ai_learning_ledger
├── requirements.txt
└── vercel.json               # Déploiement Vercel (importe api.index:app)
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

Les mises sont optimisées selon le Kelly Criterion fractionnaire (25%) :

```
Mise = Bankroll × (Edge / Odds) × 0.25
```

---

## 🛡 Gestion des Risques

- **Max Drawdown** : 15% (hard stop)
- **Position Sizing** : Kelly 25%
- **Diversification** : Multi-sport, multi-market
- **Stop Loss** : Dynamique selon volatilité

### Métriques de Performance

| Métrique | Valeur Cible |
|----------|--------------|
| Win Rate | > 65% |
| Sharpe Ratio | > 2.0 |
| Sortino Ratio | > 2.5 |
| Max Drawdown | < 15% |
| Brier Score | < 0.20 |
| Profit Factor | > 2.0 |

---

## 📊 Technologies

### Backend
- **Python 3.11** — Langage principal
- **Flask** — API web
- **Supabase** — Database PostgreSQL
- **Groq** — AI ultra-rapide (Llama 3)
- **Google Gemini** — AI contextuelle

### Frontend
- **Tailwind CSS** — Styling
- **Chart.js** — Graphiques financiers
- **Vanilla JS** — Interactivité

### DevOps
- **Vercel** — Serverless deployment
- **GitHub Actions** — CI/CD + Cron scans
- **BetterStack** — Log monitoring

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