"""
core/paim_engine.py — PAIM v7.5 — Signal validation & edge computation
"""
import difflib

from core.math_engine import calc_dnb

SPORT_LABELS = {1: "soccer", 3: "tennis", 4: "basketball"}
MAX_EDGE     = 15.0   # Hard cap — anything above is a data error, discard immediately


def convert_to_ah0(v1: float, vx: float, v2: float) -> tuple[float, float]:
    """Return (DNB_home, DNB_away) from raw 1X2 odds."""
    return calc_dnb(v1, vx), calc_dnb(v2, vx)


def strict_team_match(name_a: str, name_b: str, threshold: float = 0.72) -> bool:
    """True if both names likely refer to the same team."""
    if not name_a or not name_b:
        return True  # Cannot validate → assume OK
    a = name_a.lower().strip()
    b = name_b.lower().strip()
    if a in b or b in a:
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= threshold


MIN_EDGE = 1.5   # % — floor: anything below has no betting value


def compute_alpha(xbet_odd: float, pinnacle_price: float) -> tuple[float, str]:
    """
    Returns (edge_pct, status).
    status: "OK" — valid signal in [1.5%, 15%]
            "DISCARD" — invalid data, negative edge, or outside [MIN_EDGE, MAX_EDGE].
    """
    if not xbet_odd or not pinnacle_price or xbet_odd <= 1.01 or pinnacle_price <= 1.01:
        return 0.0, "DISCARD"
    edge = round((xbet_odd / pinnacle_price - 1) * 100, 2)
    if edge < MIN_EDGE or edge > MAX_EDGE:
        return edge, "DISCARD"
    return edge, "OK"
