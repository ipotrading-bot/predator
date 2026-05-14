"""
core/math_engine.py — PAIM v7.2 — Binary market conversion
Enforces the Zero-Draw doctrine: all soccer signals MUST be AH 0.0.
"""


def calc_dnb(odd_team: float, odd_draw: float) -> float:
    """
    Asian Handicap 0.0 synthetic price.
    Formula: Cote_DNB = Cote_Victoire × (1 − 1/Cote_Nul)
    """
    if odd_team <= 1.01 or odd_draw <= 1.01:
        return 0.0
    return round(odd_team * (1.0 - 1.0 / odd_draw), 4)


def to_binary(odds: dict, sport: str) -> tuple[float, str | None]:
    """
    Convert raw 1N2 odds to a strictly binary (2-outcome) market price.
    Returns (best_odd, market_label).

    Soccer: MUST produce AH 0.0. If no draw odd available → returns (0.0, None)
            which signals the caller to REJECT the match entirely.
    Tennis / Basketball: Moneyline (naturally binary, no draw exists).
    """
    o1 = float(odds.get("1") or 0)
    ox = float(odds.get("X") or 0)
    o2 = float(odds.get("2") or 0)

    if sport == "soccer":
        if ox <= 1.01:
            return 0.0, None  # No draw odd → DNB impossible → REJECT
        dnb_home = calc_dnb(o1, ox)
        dnb_away = calc_dnb(o2, ox)
        candidates = [v for v in [dnb_home, dnb_away] if v > 1.01]
        best = min(candidates) if candidates else 0.0
        return best, "AH 0.0"

    # Tennis / Basketball — no draw market
    candidates = [v for v in [o1, o2] if v > 1.01]
    best = min(candidates) if candidates else 0.0
    return best, "Moneyline"
