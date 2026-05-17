"""
core/constants.py — PAIM v8.5 — Single Source of Truth
All thresholds, risk classification, and Kelly calculation used by engine,
rapport, dashboard, and audit must import from here — never redefined inline.
"""

ELITE_EDGE   = 2.5    # % — VALUE / HIGH_VALUE boundary
MIN_STAKE    = 10     # € — below this Kelly stake, signal is not actionable
BANKROLL_REF = 1000   # € — reference bankroll for Telegram/Modal stake display
MAX_EDGE     = 15.0   # % — hard cap; above = data mapping error, reject
SUSPECT_EDGE = 10.0   # % — safety trigger: major sport edge above this = SUSPECT_DATA

# Fractional Kelly per sport — sharper markets = higher confidence = higher fraction
KELLY_FRACTION = {
    "basketball":       0.30,   # NBA très sharp, confiance élevée
    "hockey":           0.25,   # NHL — bon marché sharp
    "americanfootball": 0.25,   # NFL — marché liquide
    "esports":          0.22,   # Croissant, lag 1XBet notable
    "tennis":           0.20,   # Incertitude surface/fatigue
    "soccer":           0.20,   # Modèle plus incertain
    "volleyball":       0.20,
    "tabletennis":      0.20,
    "handball":         0.20,
    "rugby":            0.20,
    "baseball":         0.18,
    "mma":              0.15,   # Condition combattant incertaine
    "cricket":          0.15,
    "darts":            0.15,
    "boxing":           0.10,   # Sport le moins efficient
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
