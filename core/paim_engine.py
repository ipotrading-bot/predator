"""
core/paim_engine.py — PAIM v7.6 — Signal validation & edge computation
"""
import re
import difflib
from functools import lru_cache

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


@lru_cache(maxsize=512)
def _normalize_team(name: str) -> str:
    """Lowercase, strip club suffixes, expand common abbreviations. CACHED for 20% speedup."""
    s = name.lower().strip()
    s = _STRIP_TAGS.sub(' ', s)
    for pattern, repl in _ABBREVS:
        s = re.sub(pattern, repl, s, flags=re.I)
    return ' '.join(s.split())

SPORT_LABELS    = {1: "soccer", 3: "tennis", 4: "basketball", 5: "mma", 6: "darts", 7: "cricket", 8: "hockey"}
MAX_EDGE        = 15.0   # Hard cap — data error above this
SHARP_PROB_MIN  = 0.65   # Minimum Pinnacle devigged probability (Shin quality gate)

# Per-market probability thresholds — spreads/totals are symmetric by design
SHARP_PROB_BY_MARKET = {
    "h2h":          0.65,   # NBA/Tennis ML — strong-favourite filter
    "h2h_soccer":   0.52,   # Soccer AH 0.0 — binary by construction (~50-63%), 0.65 would block all
    "spreads":       0.52,   # Slight skew is enough (spreads price ~50/50 by construction)
    "totals":        0.52,   # Same for totals
}

_SPORT_PFX = {"basketball": "NBA", "hockey": "NHL", "tennis": "TEN", "soccer": "SOC", "boxing": "BOX", "mma": "MMA", "darts": "DRT", "cricket": "CRK"}


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


# MMA uses the same binary ML logic as boxing/tennis — no draw possible
MMA_SPORTS = {"mma", "boxing"}


def convert_to_ah0(v1: float, vx: float, v2: float) -> tuple[float, float]:
    """Return (DNB_home, DNB_away) from raw 1X2 odds."""
    return calc_dnb(v1, v2, vx), calc_dnb(v2, v1, vx)


def strict_team_match(name_a: str, name_b: str, threshold: float = 0.60) -> bool:
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


MIN_EDGE = 1.2   # % — floor (lowered for visibility — see all movements)

# ── Sharp Quartet Consensus Engine v7.8 ──────────────────────────────

_CONSENSUS_WEIGHTS: dict[str, dict[str, float]] = {
    "basketball": {"pinnacle": 0.30, "circa": 0.50, "cris": 0.10, "isn": 0.10},
    "baseball":   {"pinnacle": 0.30, "circa": 0.50, "cris": 0.10, "isn": 0.10},
    "soccer":     {"pinnacle": 0.40, "circa": 0.10, "cris": 0.20, "isn": 0.30},
    "tennis":     {"pinnacle": 0.60, "circa": 0.05, "cris": 0.25, "isn": 0.10},
}
_DEFAULT_WEIGHTS      = {"pinnacle": 0.50, "circa": 0.20, "cris": 0.20, "isn": 0.10}
_DIVERGENCE_STD_LIMIT = 0.02   # Inner Circle threshold: STD > 0.02 in decimal odds → VOLATILE


def calculate_consensus_price(
    prices_by_source: dict,
    sport: str,
) -> tuple[float, dict, bool, int]:
    """
    Weighted consensus fair price from up to 4 sharp sources.
    prices_by_source: {"pinnacle": 2.05, "circa": 2.10, "cris": 0.0, "isn": 2.07}
    Returns (consensus_price, sources_found, is_volatile, consensus_score).
    consensus_score: 0-100 — 100 = perfect agreement, 0 = at divergence limit.
    Divergence is measured as sample STD of active source prices (no pandas/scipy).
    """
    weights = (_CONSENSUS_WEIGHTS.get(sport) or _DEFAULT_WEIGHTS).copy()
    sources_found: dict[str, bool] = {}
    active: dict[str, float] = {}

    for src in ("pinnacle", "circa", "cris", "isn"):
        price = prices_by_source.get(src, 0.0)
        ok = isinstance(price, (int, float)) and float(price) > 1.01
        sources_found[src] = ok
        if ok and src in weights:
            active[src] = float(price)

    if not active:
        return 0.0, sources_found, False, 0

    vals = list(active.values())

    # STD-based divergence (Inner Circle method — no import needed)
    consensus_score = 100
    if len(vals) >= 2:
        mean = sum(vals) / len(vals)
        std  = (sum((v - mean) ** 2 for v in vals) / (len(vals) - 1)) ** 0.5
        if std > _DIVERGENCE_STD_LIMIT:
            return 0.0, sources_found, True, 0
        consensus_score = max(0, round((1 - std / _DIVERGENCE_STD_LIMIT) * 100))

    # Proportional weight redistribution for absent sources
    total_w = sum(weights[s] for s in active)
    consensus = sum(active[s] * weights[s] / total_w for s in active)
    return round(consensus, 4), sources_found, False, consensus_score


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
