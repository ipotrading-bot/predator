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

SPORT_LABELS = {1: "soccer", 3: "tennis", 4: "basketball"}
MAX_EDGE     = 15.0   # Hard cap — anything above is a data error, discard immediately


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
