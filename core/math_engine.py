"""
core/math_engine.py — PAIM v7.5 — Binary market conversion
Enforces Zero-Draw doctrine: Soccer = AH 0.0 only.
"""


def calc_dnb(odd_team: float, odd_draw: float) -> float:
    """AH 0.0: Cote_DNB = Cote_Victoire × (1 − 1/Cote_Nul)"""
    if odd_team <= 1.01 or odd_draw <= 1.01:
        return 0.0
    return round(odd_team * (1.0 - 1.0 / odd_draw), 4)


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
        if ox <= 1.01:
            return 0.0, None, ""  # No draw odd → DNB impossible → REJECT
        dnb_home = calc_dnb(o1, ox)
        dnb_away = calc_dnb(o2, ox)
        if dnb_home > 1.01 and (dnb_away <= 1.01 or dnb_home <= dnb_away):
            return dnb_home, "AH 0.0", home
        elif dnb_away > 1.01:
            return dnb_away, "AH 0.0", away
        return 0.0, None, ""

    # Tennis / Basketball — no draw market
    if o1 > 1.01 and (o2 <= 1.01 or o1 <= o2):
        return o1, "Moneyline", home
    elif o2 > 1.01:
        return o2, "Moneyline", away
    return 0.0, "Moneyline", ""
