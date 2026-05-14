"""
core/paim_engine.py — PAIM v7.2 — Signal validation & edge classification
"""
import difflib

SPORT_LABELS = {1: "soccer", 3: "tennis", 4: "basketball"}
SUSPECT_EDGE = 15.0   # Flag ⚠️ SUSPECT DATA — not shown as ELITE
MAX_EDGE     = 25.0   # Discard entirely — data mapping error


def strict_team_match(name_a: str, name_b: str, threshold: float = 0.72) -> bool:
    """True if both names likely refer to the same team."""
    a = name_a.lower().strip()
    b = name_b.lower().strip()
    if a in b or b in a:
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= threshold


def compute_alpha(xbet_odd: float, pinnacle_price: float) -> tuple[float, str]:
    """
    Returns (edge_pct, status).
    status values:
      "OK"      — valid signal, edge within normal range
      "SUSPECT" — edge > 15%: show on dashboard with warning, never ELITE
      "DISCARD" — edge > 25% or invalid inputs: drop entirely
    """
    if not xbet_odd or not pinnacle_price or xbet_odd <= 1.01 or pinnacle_price <= 1.01:
        return 0.0, "DISCARD"
    edge = round((xbet_odd / pinnacle_price - 1) * 100, 2)
    if abs(edge) > MAX_EDGE:
        return edge, "DISCARD"
    if abs(edge) > SUSPECT_EDGE:
        return edge, "SUSPECT"
    return edge, "OK"
