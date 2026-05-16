"""
core/constants.py — PAIM v8.5 — Single Source of Truth
All thresholds, risk classification, and Kelly calculation used by engine,
rapport, dashboard, and audit must import from here — never redefined inline.
"""

ELITE_EDGE   = 2.5    # % — VALUE / HIGH_VALUE boundary
MIN_STAKE    = 10     # € — below this Kelly stake, signal is not actionable
BANKROLL_REF = 1000   # € — reference bankroll for Telegram/Modal stake display
MAX_EDGE     = 15.0   # % — hard cap; above = data mapping error, reject


def risk_flag(edge_pct: float) -> str:
    """Consistent risk label stored in DB and used by all consumers."""
    if edge_pct >= ELITE_EDGE * 2:   # >= 5.0 %
        return "HIGH_VALUE"
    if edge_pct >= ELITE_EDGE:        # >= 2.5 %
        return "VALUE"
    return "LOW_VALUE"


def kelly_stake(xbet_odd: float, sharp_prob: float,
                bankroll: int = BANKROLL_REF) -> int:
    """
    Fractional Kelly ×0.25, rounded to nearest integer.
    Returns 0 (non-actionable) if computed stake < MIN_STAKE.
    """
    b = xbet_odd - 1
    if b <= 0 or sharp_prob <= 0:
        return 0
    kf = (sharp_prob * b - (1 - sharp_prob)) / b
    stake = round(max(0.0, kf * 0.25) * bankroll)
    return stake if stake >= MIN_STAKE else 0
