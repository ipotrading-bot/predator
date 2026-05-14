"""
core/paim_engine.py — PAIM v7.2 — Strict match validation & edge computation
"""
import difflib

SPORT_LABELS = {1: "soccer", 3: "tennis", 4: "basketball"}
MAX_EDGE = 25.0  # Sanity cap — above this is a data mapping error, not a signal


def strict_team_match(name_a: str, name_b: str, threshold: float = 0.72) -> bool:
    """True if both names likely refer to the same team (difflib ratio)."""
    a = name_a.lower().strip()
    b = name_b.lower().strip()
    if a in b or b in a:
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= threshold


def sanity_check(edge_pct: float) -> bool:
    """Return False (discard signal) when edge exceeds MAX_EDGE — signals a data error."""
    return abs(edge_pct) <= MAX_EDGE


def calc_dnb(odd_team: float, odd_draw: float) -> float:
    """
    Draw No Bet (Asian Handicap 0.0) fair price.
    DNB = odd_team × (1 − 1/odd_draw)
    """
    if odd_team <= 1.01 or odd_draw <= 1.01:
        return 0.0
    return round(odd_team * (1.0 - 1.0 / odd_draw), 4)


def compute_alpha(xbet_odd: float, pinnacle_price: float) -> float:
    """
    Edge vs Pinnacle fair price.
    Returns 0.0 if inputs are invalid or the result fails the sanity check (> 25%).
    """
    if not xbet_odd or not pinnacle_price or xbet_odd <= 1.01 or pinnacle_price <= 1.01:
        return 0.0
    edge = round((xbet_odd / pinnacle_price - 1) * 100, 2)
    return edge if sanity_check(edge) else 0.0
