"""
core/constants.py — PAIM v8.5 — Single Source of Truth
All thresholds, risk classification, and Kelly calculation used by engine,
rapport, dashboard, and audit must import from here — never redefined inline.
"""

ELITE_EDGE           = 2.5    # % — VALUE / HIGH_VALUE boundary (NBA, hockey, etc.)
SOCCER_ELITE_EDGE    = 1.5    # % — Soccer AH0 : marché plus serré, VALUE dès 1.5%
AH0_VALUE_THRESHOLD  = 1.5    # Soccer DNB : favori à 1.5+ = valeur intrinsèque
PURGE_EDGE_FLOOR     = 0.5    # % — floor purge : ne jamais supprimer au-dessus de ça
MIN_STAKE    = 2      # € — below this Kelly stake, signal is not actionable
BANKROLL_REF = 150    # € — 100 000 XOF (taux fixe 655.96 XOF/€)
MAX_EDGE     = 15.0   # % — hard cap; above = data mapping error, reject
SUSPECT_EDGE = 10.0   # % — safety trigger: major sport edge above this = SUSPECT_DATA (cap totals=15%)

# ── Retry & Rate Limiting (Centralized) ──────────────────────────────
DELAY_XBET_MIN       = 2.0      # Seconds — min delay between 1XBet requests
DELAY_XBET_MAX       = 5.0      # Seconds — max delay (random jitter)
DELAY_GEMINI_RATE    = 65.0     # Seconds — Gemini rate limit backoff
DELAY_DB_RETRY       = 1.0      # Seconds — Supabase transient error retry
MAX_DB_RETRIES       = 3        # Attempts — max retries before giving up
GLOBAL_TIMEOUT       = 540      # Seconds — 9 minutes, safety net for GitHub Actions
DEBUG_MODE           = False    # Will be set from env var PREDATOR_DEBUG

# Fractional Kelly par sport — uniquement les 11 sports actifs sélectionnés
# Principe : sharper market = fraction plus haute = mise plus agressive
# Sports exclus (cricket, darts, boxing, tennis, etc.) → retirés pour réduire bruit
KELLY_FRACTION = {
    "basketball":  0.30,   # NBA — marché le plus sharp au monde, confiance max
    "hockey":      0.25,   # NHL — sharp, liquid, peu de bruit
    "soccer":      0.22,   # FIFA WC + Copa Lib + MLS + Brasileirão — relevé légèrement
    "baseball":    0.22,   # MLB + KBO + NPB — volume élevé + lag timezone documenté
    "rugbyleague": 0.20,   # NRL — Pinnacle très sharp, marché australien fiable
    "aussierules": 0.20,   # AFL — Pinnacle + Betfair très actifs
}


def risk_flag(edge_pct: float) -> str:
    """Consistent risk label stored in DB and used by all consumers."""
    if edge_pct >= ELITE_EDGE * 2:   # >= 5.0 %
        return "HIGH_VALUE"
    if edge_pct >= ELITE_EDGE:        # >= 2.5 %
        return "VALUE"
    return "LOW_VALUE"


def kelly_stake(xbet_odd: float, sharp_prob: float,
                bankroll: int = BANKROLL_REF,
                sport: str = "soccer") -> int:
    """
    Fractional Kelly adaptatif par sport — fraction dans KELLY_FRACTION.
    Returns 0 (non-actionable) if computed stake < MIN_STAKE.
    """
    b = xbet_odd - 1
    if b <= 0 or sharp_prob <= 0:
        return 0
    kf = (sharp_prob * b - (1 - sharp_prob)) / b
    fraction = KELLY_FRACTION.get(sport, 0.20)
    stake = round(max(0.0, kf * fraction) * bankroll)
    return stake if stake >= MIN_STAKE else 0
