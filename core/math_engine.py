"""
core/math_engine.py — PAIM v9.0 — Binary market conversion + Power devigging
Enforces Zero-Draw doctrine: Soccer = AH 0.0 only.

Devigging method: Power (multiplicative) instead of Additive.
- Additive: p_i = q_i / Σq_j  (draw cancels in DNB — algebraically ignored)
- Power:    p_i = q_i^(1/k) / Σq_j^(1/k)  where k = Σq_j (overround)
  The draw odd influences the result in Power, giving more accurate win/lose
  probabilities, especially for asymmetric markets (heavy favourites).
"""


def _power_devig3(o1: float, o2: float, o3: float) -> tuple[float, float, float]:
    """
    Power devigging for 3-way markets.
    Returns (p1, p2, p3) true probabilities summing to 1.0.
    Returns (0,0,0) on invalid input.
    """
    if o1 <= 1.01 or o2 <= 1.01 or o3 <= 1.01:
        return 0.0, 0.0, 0.0
    q1, q2, q3 = 1.0 / o1, 1.0 / o2, 1.0 / o3
    k = q1 + q2 + q3          # overround (> 1.0)
    if k <= 0.0:
        return 0.0, 0.0, 0.0
    exp = 1.0 / k              # power exponent shrinks implied probs toward 1
    r1, r2, r3 = q1 ** exp, q2 ** exp, q3 ** exp
    total = r1 + r2 + r3
    if total <= 0.0:
        return 0.0, 0.0, 0.0
    return r1 / total, r2 / total, r3 / total


def calc_dnb(odd_team: float, odd_other: float, odd_draw: float) -> float:
    """
    AH 0.0 / Draw No Bet — Power devigging (replaces additive).

    With additive devigging, odd_draw algebraically cancels: DNB = 1 + o_team/o_other.
    Power devigging retains the draw's contribution, giving more accurate prices
    for asymmetric markets (error up to 1.5% on heavy favourites with additive).

    Returns 0.0 on invalid input.
    """
    pt, po, _ = _power_devig3(odd_team, odd_other, odd_draw)
    if pt <= 0.0 or po <= 0.0:
        return 0.0
    denom = pt + po
    if denom <= 0.0:
        return 0.0
    return round(denom / pt, 4)


def devig_prob(own_odd: float, other_odd: float) -> float:
    """
    Power-normalized binary probability — removes bookmaker margin.
    For binary markets (spreads, totals, tennis/NBA ML, DNB-vs-DNB).

    Power method: p = q^(1/k) / (q1^(1/k) + q2^(1/k))
    More accurate than additive for asymmetric prices (e.g. 1.10 vs 8.00).
    Returns 0.0 if either odd is invalid.
    """
    if own_odd <= 1.01 or other_odd <= 1.01:
        return 0.0
    q1, q2 = 1.0 / own_odd, 1.0 / other_odd
    k = q1 + q2
    if k <= 0.0:
        return 0.0
    exp = 1.0 / k
    r1, r2 = q1 ** exp, q2 ** exp
    total = r1 + r2
    if total <= 0.0:
        return 0.0
    return round(r1 / total, 4)


def is_round_number_line(point: float) -> bool:
    """True if a totals line is a whole number (e.g. 8.0, 9.0) — these can
    push. Half-lines (.5) never push — P(push)=0, no adjustment needed."""
    return bool(point) and point > 0 and point % 1 == 0


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
        # Use RAW 1X2 odds to pick the favourite — DNB formula can fail
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
