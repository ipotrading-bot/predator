"""
core/paim_engine.py — PAIM v7.6 — Signal validation & edge computation
"""
import re
import difflib

from core.math_engine import calc_dnb

# Common abbreviations that cause Pinnacle ↔ 1XBet name divergence
_ABBREVS = [
    (r'\bman\s*utd\.?\b',            'manchester united'),
    (r'\bm\.?\s*united\b',           'manchester united'),
    (r'\bpsg\b',                      'paris'),
    (r'\bspurs\b',                    'tottenham'),
    (r'\br\.\s+(?=madrid|sociedad)', 'real '),
    (r'\binter\s+milan\b',            'internazionale'),
]
_STRIP_TAGS = re.compile(r'\s*\b(fc|cf|sc|ac|gfc|afc|fk|sk|bk|rfc|sfc)\b\s*', re.I)


def _normalize_team(name: str) -> str:
    """Lowercase, strip club suffixes, expand common abbreviations."""
    s = name.lower().strip()
    s = _STRIP_TAGS.sub(' ', s)
    for pattern, repl in _ABBREVS:
        s = re.sub(pattern, repl, s, flags=re.I)
    return ' '.join(s.split())

SPORT_LABELS    = {1: "soccer", 3: "tennis", 4: "basketball"}
MAX_EDGE        = 15.0   # Hard cap — data error above this
SHARP_PROB_MIN  = 0.65   # Minimum Pinnacle devigged probability (Shin quality gate)

# Per-market probability thresholds — spreads/totals are symmetric by design
SHARP_PROB_BY_MARKET = {
    "h2h":     0.65,   # Strong-favourite filter
    "spreads": 0.52,   # Slight skew is enough (spreads price ~50/50 by construction)
    "totals":  0.52,   # Same for totals
}

_SPORT_PFX = {"basketball": "NBA", "tennis": "TEN", "soccer": "SOC"}


def market_label(key: str, side: str, point: float, sport: str) -> str:
    """Human-readable market label for Dashboard and Telegram."""
    pfx = _SPORT_PFX.get(sport, sport[:3].upper())
    if key == "totals":
        sign = f" {point}" if point else ""
        return f"{pfx} {side.capitalize()}{sign}"
    if key == "spreads":
        sign = f"+{point}" if point > 0 else str(point)
        return f"{pfx} PS {sign}"
    # h2h
    return "AH 0.0" if sport == "soccer" else f"{pfx} ML"


def convert_to_ah0(v1: float, vx: float, v2: float) -> tuple[float, float]:
    """Return (DNB_home, DNB_away) from raw 1X2 odds."""
    return calc_dnb(v1, vx), calc_dnb(v2, vx)


def strict_team_match(name_a: str, name_b: str, threshold: float = 0.72) -> bool:
    """True if both names likely refer to the same team (handles abbreviations)."""
    if not name_a or not name_b:
        return True
    a = name_a.lower().strip()
    b = name_b.lower().strip()
    if a in b or b in a:
        return True
    na = _normalize_team(a)
    nb = _normalize_team(b)
    if na and nb and (na in nb or nb in na):
        return True
    return difflib.SequenceMatcher(None, na, nb).ratio() >= threshold


MIN_EDGE = 1.5   # % — floor: anything below has no betting value


def compute_alpha(
    xbet_odd: float,
    pinnacle_price: float,
    min_edge: float = MIN_EDGE,
) -> tuple[float, str]:
    """
    Returns (edge_pct, status).
    status: "OK"      — valid signal in [min_edge, MAX_EDGE]
            "DISCARD" — invalid data, negative edge, or outside thresholds.
    min_edge defaults to the global MIN_EDGE (1.5 %) but can be overridden
    by the learning_layer for sport-specific adaptive thresholds.
    """
    if not xbet_odd or not pinnacle_price or xbet_odd <= 1.01 or pinnacle_price <= 1.01:
        return 0.0, "DISCARD"
    edge = round((xbet_odd / pinnacle_price - 1) * 100, 2)
    if edge < min_edge or edge > MAX_EDGE:
        return edge, "DISCARD"
    return edge, "OK"
