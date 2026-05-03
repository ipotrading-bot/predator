# 🦅 PREDATOR PAIM v2.0 — Hedge Fund Sportif Autonome

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
| `ODDS_API_KEY` | The-Odds API key | ✅ |
| `GEMINI_API_KEY` | Google Gemini API key | ✅ |
| `SUPABASE_URL` | Supabase project URL | ✅ |
| `SUPABASE_KEY` | Supabase anon key | ✅ |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token | ✅ |
| `TELEGRAM_CHAT_ID` | Telegram chat/channel ID | ✅ |
| `GROQ_API_KEY` | Groq API key (Llama 3) | ❌ |
| `NEWS_API_KEY` | NewsAPI.org key | ❌ |
| `BETTERSTACK_TOKEN` | BetterStack log token | ❌ |
| `PREDATOR_SECRET` | API auth secret | ❌ |

### Obtenir les Clés API

1. **The-Odds API** — [the-odds-api.com](https://the-odds-api.com/)
2. **Google Gemini** — [aistudio.google.com](https://aistudio.google.com/)
3. **Groq** — [console.groq.com](https://console.groq.com/) (Gratuit !)
4. **NewsAPI** — [newsapi.org](https://newsapi.org/) (Plan gratuit)
5. **Supabase** — [supabase.com](https://supabase.com/) (Plan gratuit)
6. **BetterStack** — [betterstack.com/logs](https://betterstack.com/logs) (Plan gratuit)

---

## 📡 API Endpoints

### Core

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Dashboard HTML |
| `/api/stats` | GET | Statistiques du bot |
| `/api/signals/live` | GET | Signaux en cours (7/9 system) |
| `/api/scan` | POST | Déclencher un scan PAIM |
| `/api/health` | GET | Health check des services |

### Analytics

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/audit/metrics` | GET | Métriques avancées (Sharpe, Brier, etc.) |
| `/api/equity-curve` | GET | Données courbe d'équité |
| `/api/ledger` | GET | Historique des transactions |
| `/api/report` | GET | Rapport de performance (JSON) |
| `/api/report/pdf` | GET | Rapport PDF téléchargeable |

### Intelligence

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/groq/status` | GET | Statut Groq AI |
| `/api/groq/filter` | POST | Filtrage rapide d'un signal |
| `/api/news` | GET | News sportives |
| `/api/news/context` | GET | Analyse contexte news pour un signal |
| `/api/news/market-moving` | GET | News impactant les cotes |

### System

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/exposure` | GET | Exposition actuelle |
| `/api/ticker` | GET | Market ticker items |
| `/api/logs/recent` | GET | Logs récents du scan |

---

## 📁 Structure du Projet

```
predator/
├── api/
│   ├── index.py          # Flask main app
│   ├── analytics.py      # QuantStats integration
│   ├── audit.py          # Audit endpoint
│   ├── gemini_client.py  # Google Gemini client
│   ├── groq_client.py    # Groq AI client (NEW)
│   ├── health.py         # Health check
│   ├── logger.py         # BetterStack logging (NEW)
│   ├── news_client.py    # News API integration (NEW)
│   ├── odds_client.py    # Odds fetcher
│   ├── rate_limiter.py   # API rate limiting
│   ├── scan.py           # Scan trigger endpoint
│   ├── signals.py        # Live signals endpoint
│   ├── supabase_client.py # Database client
│   └── telegram_client.py # Telegram notifications
├── core/
│   ├── math_engine.py    # Calculs mathématiques
│   ├── math_logic.py     # Logique PAIM
│   ├── notifications.py  # Système de notifications
│   ├── paim_engine.py    # Moteur principal PAIM
│   ├── risk_manager.py   # Gestion des risques
│   └── signal_validator.py # Validation des signaux
├── data/
│   ├── odds_fetcher.py   # Récupération des cotes
│   └── supabase_client.py # Client database
├── signals/
│   ├── obfuscator.py     # Obfuscation des signaux
│   └── scanner.py        # Scanner de marché
├── templates/
│   └── index.html        # Dashboard HTML (NEW UI)
├── tgbot/
│   ├── __init__.py
│   └── bot.py            # Telegram bot
├── config.py             # Configuration centralisée
├── main.py               # Entry point
├── requirements.txt      # Python dependencies
└── vercel.json           # Vercel deployment config
```

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