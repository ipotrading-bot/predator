# PREDATOR PAIM v8.5 — Protocole Définitif

## AUDIT DE SYNCHRONISATION — RÉSULTATS

### 1. DOCTRINE MATHÉMATIQUE

**Arbitrage Lead-Lag.** On n'est pas un parieur, on est un arbitragiste de l'asymétrie d'information. Le bot détecte quand 1XBet (Soft) n'a pas encore ajusté sa cote sur un mouvement Sharp détecté chez Pinnacle.

**Binary Synthesis — Zero Draw Policy (NON-NÉGOCIABLE) :**
- Soccer → `AH 0.0` obligatoire via `calc_dnb(odd_victoire, odd_nul)`
  - Formule : `Cote_DNB = Cote_Victoire × (1 − 1/Cote_Nul)`
  - Si cote du nul absente → REJECT immédiat (`to_binary` retourne `0.0`)
- NBA / Tennis → Moneyline (naturellement binaire, pas de nul)

**Shin Method :** Dévigage via `devig_prob(p_own, p_other)` :
```
p_true = (1/own) / (1/own + 1/other)
```

**Kelly Fractionnaire 0.25 :**
```
KF = (p × b − (1−p)) / b   ×   0.25
Mise = round(KF × 1000€)   (minimum 10€ sinon non-actionnable)
```

**Thresholds :**
| Paramètre | Valeur |
|---|---|
| MIN_EDGE | 1.5% |
| ELITE_EDGE | 2.5% |
| MAX_EDGE (Hard Cap) | 15.0% → rejet (erreur mapping) |
| HIGH_VALUE boundary | ≥ 5.0% |

**SHARP_PROB_BY_MARKET :**
| Marché | Seuil |
|---|---|
| h2h NBA/Tennis | 0.65 (filtre favori fort) |
| h2h_soccer (AH 0.0) | 0.52 (naturellement ~50-63%) |
| spreads | 0.52 |
| totals | 0.52 |

---

### 2. SOURCES DE DONNÉES

#### Soft (1XBet)
- **Primaire :** 1XBet JSON direct — 6 URL variants, délai aléatoire 2-5s anti-ban
- **Fallback :** Gemini 2.5 Flash Lite — génère les cotes 1XBet (connaissance interne, sans web search)

#### Sharp (Pinnacle)
- **Tier 1 — The Odds API (PRIORITÉ ABSOLUE) :** `core/odds_api.py` → real 1XBet + Pinnacle pour le même event, fenêtre 72h. Actif via `ODDS_API_KEY`.
- **Tier 2 — Gemini 2.0 Flash + Google Search :** `fetch_pinnacle_prices()` → interroge Pinnacle via Google Search. Si Pinnacle absent → Betfair Exchange avec pénalité -0.5%.
- **Tier 3 — Gemini Estimateur :** `fetch_estimated_prices()` → connaissance interne, marge 2% appliquée (pas de l'arbitrage pur, value betting).
- **Oracle :** `get_pinnacle_price()` → max 3 appels par scan pour matchs sans prix Gemini.

**Pipeline Tier :** `OddsAPI → Gemini/Pinnacle+Search → Gemini/Estimateur`

---

### 3. CONTRAINTES TECHNIQUES DE DÉPLOIEMENT

#### GitHub Actions (Engine)
- `run_engine.py` : toutes les 30 min EU 06:00–21:30 UTC, toutes les heures overnight (NBA)
- `run_audit.py` : cron 00:00 / 06:00 / 12:00 / 18:00 UTC
- `run_rapport.py` : cron 06:05 et 18:05 UTC (`rapport.yml`)
- Secrets : `SUPABASE_URL`, `SUPABASE_KEY`, `GEMINI_API_KEY`, `ODDS_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`

#### Vercel (Dashboard)
- `api/index.py` — Flask, 3 onglets : Dashboard / Ledger / Audit CLV
- Aesthetic Bloomberg Terminal
- Toutes les variables d'environnement aussi configurées dans Vercel

#### Supabase (Database)
- Project : `chnyxeyqpdipeogirrpu`
- Tables : `signals`, `meta`, `ledger`, `audit`
- Purge automatique avant chaque scan : pending + past match_time + >48h + edge>15% + stale

#### Contraintes techniques dures
- **Pas de scipy, pas de pandas** — pur Python / Numpy uniquement
- **Pas de mocks** — intégration réelle uniquement
- No local-time contamination — tous les timestamps en UTC/GMT

---

### 4. FICHIERS CRITIQUES DU MOTEUR PAIM

| Fichier | Rôle |
|---|---|
| `core/paim_engine.py` | `SHARP_PROB_BY_MARKET`, `compute_alpha`, `strict_team_match`, `market_label` |
| `core/math_engine.py` | `calc_dnb` (AH 0.0), `devig_prob` (Shin), `to_binary` |
| `core/constants.py` | Single Source of Truth — `ELITE_EDGE`, `MIN_STAKE`, `kelly_stake`, `risk_flag` |
| `core/harvester.py` | `fetch_matches` (1XBet→Gemini), `fetch_pinnacle_prices` (Gemini Flash+Google), `fetch_estimated_prices` |
| `core/odds_api.py` | Tier 1 — The Odds API (72h window, Pinnacle réel) |
| `core/oracle.py` | Prix Pinnacle unitaire (MAX 3/scan) |
| `core/audit_engine.py` | CLV audit — closing line value |
| `core/settlement.py` | Settlement des signaux après match |
| `core/learning_layer.py` | Thresholds dynamiques par sport |
| `run_engine.py` | Pipeline complet + Portfolio Balancer |
| `api/index.py` | Vercel Flask routes |

#### Portfolio Balancer
- Quota : max 3 signaux par sport par scan (`soccer:3, basketball:3, tennis:3`)
- Tri : alpha décroissant (`edge_pct` DESC) — un +5% NBA bat un +3% soccer
- Ordre Telegram : basketball → tennis → soccer

---

### 5. ÉTAT DE SYNCHRONISATION — VERDICT

| Point | Statut |
|---|---|
| Doctrine Lead-Lag | CONFORME |
| Binary Synthesis (AH 0.0 / ML) | CONFORME — `to_binary()` + `calc_dnb()` |
| Shin Method | CONFORME — `devig_prob()` |
| Kelly 0.25 | CONFORME — `core/constants.py` |
| Source Soft : 1XBet JSON direct | CONFORME — 6 URL variants |
| Source Sharp : Gemini 2.0 Flash + Google | CONFORME — `fetch_pinnacle_prices()` |
| Tier 1 : OddsAPI actif | CONFORME — `ODDS_API_KEY` configuré, fenêtre 72h |
| Hard Cap 15% | CONFORME — `MAX_EDGE = 15.0` |
| h2h_soccer threshold 0.52 | CONFORME — fix appliqué 2026-05-15 |
| No scipy / No pandas | CONFORME |

---

*Généré automatiquement par audit — PREDATOR PAIM v8.5 — 2026-05-16*
