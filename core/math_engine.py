"""
core/math_engine.py — PAIM v8.0 — Binary market conversion + Shin devigging
Enforces Zero-Draw doctrine: Soccer = AH 0.0 only.
New: devig_prob() for spreads/totals quality filter.
"""


def calc_dnb(odd_team: float, odd_other: float, odd_draw: float) -> float:
    """
    AH 0.0 / Draw No Bet — devig 3-way correct.
    Old formula (odd_team × (1-1/odd_draw)) gave DNB < 1.0 for strong favorites,
    causing the system to always fall back to the home team. Fixed: use all 3 odds.
    """
    if odd_team <= 1.01 or odd_other <= 1.01 or odd_draw <= 1.01:
        return 0.0
    p_team  = 1.0 / odd_team
    p_other = 1.0 / odd_other
    p_draw  = 1.0 / odd_draw
    total   = p_team + p_other + p_draw
    if total <= 0:
        return 0.0
    pt = p_team / total
    po = p_other / total
    denom = pt + po
    if denom <= 0:
        return 0.0
    return round(1.0 / (pt / denom), 4)


def devig_prob(own_odd: float, other_odd: float) -> float:
    """
    Shin-normalized binary probability — removes bookmaker margin.
    For binary markets (spreads, totals, tennis/NBA h2h):
    p_true = (1/own) / (1/own + 1/other)
    Returns 0.0 if either odd is invalid.
    """
    if own_odd <= 1.01 or other_odd <= 1.01:
        return 0.0
    q1 = 1.0 / own_odd
    q2 = 1.0 / other_odd
    return round(q1 / (q1 + q2), 4)


def to_binary(odds: dict, sport: str, home: str = "", away: str = "") -> tuple[float, str | None, str]:
    """
    Convert raw 1N2 odds to a binary market price.
    Returns (best_odd, market_label, favorite_team_name).

    Soccer: MUST produce AH 0.0. No draw odd → (0.0, None, "") → REJECT.
    Tennis / Basketball: Moneyline (naturally binary).
    """
    o1 = float(odds.get("1") or 0)
    ox = float(odds.get("X") or 0)
    o2 = float(odds.get("2") or 0)

    if sport == "soccer":
        if ox <= 1.01 or o1 <= 1.01 or o2 <= 1.01:
            return 0.0, None, ""
        dnb_home = calc_dnb(o1, o2, ox)
        dnb_away = calc_dnb(o2, o1, ox)
        # Use RAW 1X2 odds to pick the favorite — DNB formula can still fail
        # for extreme edge-cases, but raw odds never lie.
        if o1 <= o2:
            return (dnb_home, "AH 0.0", home) if dnb_home > 1.01 else (0.0, None, "")
        else:
            return (dnb_away, "AH 0.0", away) if dnb_away > 1.01 else (0.0, None, "")

    # Tennis / Basketball — no draw market
    if o1 > 1.01 and (o2 <= 1.01 or o1 <= o2):
        return o1, "Moneyline", home
    elif o2 > 1.01:
        return o2, "Moneyline", away
    return 0.0, "Moneyline", ""
