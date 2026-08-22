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

SPORT_LABELS    = {1: "soccer", 3: "tennis", 4: "basketball", 5: "mma", 6: "darts", 7: "cricket", 8: "hockey", 16: "aussierules", 17: "rugbyleague"}
MAX_EDGE        = 15.0   # Hard cap — data error above this
SHARP_PROB_MIN  = 0.55   # Minimum Pinnacle devigged probability (abaissé — Finales NBA ~60/40)

# Per-market probability thresholds — spreads/totals are symmetric by design
SHARP_PROB_BY_MARKET = {
    "h2h":          0.55,   # NBA/Hockey ML — 0.65 bloquait toutes les Finales NBA compétitives
    "h2h_soccer":   0.50,   # Soccer AH 0.0 — seuil binaire pur
    "spreads":       0.50,   # Seuil binaire pur — symétrique par construction
    "totals":        0.50,   # Idem totals
}

_SPORT_PFX = {"basketball": "NBA", "hockey": "NHL", "tennis": "TEN", "soccer": "SOC", "boxing": "BOX", "mma": "MMA", "darts": "DRT", "cricket": "CRK", "aussierules": "AFL", "rugbyleague": "NRL", "baseball": "MLB", "rugby": "RUG", "americanfootball": "NFL", "euroleague_basketball": "EUL", "college_football": "CFB"}


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


def correlation_group(sport: str, league: str, match_time: str) -> str:
    """
    Tag identifying signals whose outcomes are NOT safely assumed
    independent (Task 5) — same sport/league/kickoff-date. Two markets on
    the same match always share this tag (trivially correlated); two
    different matches in the same league on the same day are a coarser
    but still real correlation (shared referee pool, weather, schedule
    congestion, etc). core/tax_engine.py's suggest_system() refuses by
    default to combine two legs sharing this tag into one accumulator.
    """
    date = (match_time or "")[:10]
    return f"{sport}:{date}:{league}"


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
    # No raw pre-normalization substring shortcut here on purpose: a plain
    # `a in b or b in a` on un-stripped names (before tag-stripping/abbrev
    # expansion below) matches on arbitrary short substrings — e.g. a
    # generic token shared by two unrelated clubs in different leagues —
    # and would silently bind one match's odds to a different match.
    # Requiring normalization first (tag-strip + abbrev expand) before any
    # containment check is strictly safer and covers the same legitimate
    # cases (e.g. "Barcelona" vs "FC Barcelona").
    na = _normalize_team(a)
    nb = _normalize_team(b)
    if na and nb and (na in nb or nb in na):
        return True
    return difflib.SequenceMatcher(None, na, nb).ratio() >= threshold


def resolve_selection_side(selection: str, home: str, away: str) -> bool | None:
    """
    Which side of a "home vs away" pair does `selection` name? True=home,
    False=away, None=unresolvable. Exact (case/space-insensitive) equality
    wins first: two clubs sharing a token ("America MG" / "America RN")
    look alike to strict_team_match's fuzzy ratio, but an exact selection
    string is unambiguous. Fuzzy matching only decides when it matches
    exactly ONE side — a selection both teams match (shared token; or an
    empty string, which strict_team_match treats as a wildcard) or neither
    matches returns None, so callers can refuse to guess instead of
    settling/CLV-grading the wrong side.
    """
    sel = (selection or "").lower().strip()
    h   = (home or "").lower().strip()
    a   = (away or "").lower().strip()
    if not sel or not h or not a:
        return None
    if sel == h and sel != a:
        return True
    if sel == a and sel != h:
        return False
    home_matches = strict_team_match(sel, h)
    away_matches = strict_team_match(sel, a)
    if home_matches == away_matches:
        return None
    return home_matches


MIN_EDGE = 1.2   # % — floor (lowered for visibility — see all movements)

# ── Sharp Quartet Consensus Engine v7.8 ──────────────────────────────

_CONSENSUS_WEIGHTS: dict[str, dict[str, float]] = {
    "basketball": {"pinnacle": 0.30, "circa": 0.50, "cris": 0.10, "isn": 0.10},
    "euroleague_basketball": {"pinnacle": 0.30, "circa": 0.50, "cris": 0.10, "isn": 0.10},  # mêmes mécaniques
    "baseball":   {"pinnacle": 0.30, "circa": 0.50, "cris": 0.10, "isn": 0.10},
    "soccer":     {"pinnacle": 0.40, "circa": 0.10, "cris": 0.20, "isn": 0.30},
    "tennis":     {"pinnacle": 0.60, "circa": 0.05, "cris": 0.25, "isn": 0.10},
}
_DEFAULT_WEIGHTS   = {"pinnacle": 0.50, "circa": 0.20, "cris": 0.20, "isn": 0.10}
# CV = STD / mean — scale-invariant divergence measure.
# STD absolue de 0.02 sur cote 1.10 = +1.8% écart (énorme),
# même 0.02 sur cote 3.50 = +0.6% (normal). CV corrige ce biais.
_DIVERGENCE_CV_LIMIT = 0.012   # 1.2% CV → équivalent à STD 0.02 sur cote ~1.67


def calculate_consensus_price(
    prices_by_source: dict,
    sport: str,
) -> tuple[float, dict, bool, int]:
    """
    Weighted consensus fair price from up to 4 sharp sources.
    prices_by_source: {"pinnacle": 2.05, "circa": 2.10, "cris": 0.0, "isn": 2.07}
    Returns (consensus_price, sources_found, is_volatile, consensus_score).
    consensus_score: 0-100 — 100 = perfect agreement, 0 = at CV limit.

    Divergence: Coefficient of Variation (STD/mean) — scale-invariant.
    Prevents false VOLATILE rejection on high-odds underdogs where absolute
    STD of 0.02 is meaningless at odds 3.0+ but was blocking valid signals.
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

    consensus_score = 100
    if len(vals) >= 2:
        mean = sum(vals) / len(vals)
        # Sample STD (Bessel correction)
        std  = (sum((v - mean) ** 2 for v in vals) / (len(vals) - 1)) ** 0.5
        cv   = std / mean if mean > 0 else 0.0   # Coefficient of Variation
        if cv > _DIVERGENCE_CV_LIMIT:
            return 0.0, sources_found, True, 0
        consensus_score = max(0, round((1 - cv / _DIVERGENCE_CV_LIMIT) * 100))

    # Proportional weight redistribution for absent sources
    total_w = sum(weights[s] for s in active)
    consensus = sum(active[s] * weights[s] / total_w for s in active)
    return round(consensus, 4), sources_found, False, consensus_score


def compute_alpha(
    xbet_odd: float,
    sharp_prob: float,
    min_edge: float = MIN_EDGE,
) -> tuple[float, str]:
    """
    Rend (edge_pct, status) où edge_pct est l'ESPÉRANCE DE GAIN VRAIE :
        edge = (sharp_prob × cote_soft − 1) × 100
    avec sharp_prob la probabilité dévigorisée (ensemble de
    core.math_engine.devig). status: "OK" dans [min_edge, MAX_EDGE],
    "DISCARD" sinon.

    HISTORIQUE — jusqu'au 2026-08-22 cette fonction rendait le RATIO DE PRIX
    (xbet/pinnacle − 1) contre une cote Pinnacle encore vigorisée : la marge
    du book sharp (~2 %) comptait comme de l'edge, et la distorsion explosait
    à cote courte (un 1,08 contre 1,02 affichait +5,9 % pour une EV réelle de
    −7,3 %). Mesuré sur le ledger réglé au 2026-08-22 : Brier de sharp_prob
    pire que la proba implicite brute du book soft, pente de recalibration
    0,12, CLV réel nul (+0,18 %, t=0,18). Ne pas revenir au ratio de prix.

    Toujours PAS de gate fiscal ici (voir l'historique git autour du
    2026-07-08) : core.tax_engine.suggest_system()/is_combo_tax_viable()
    reste le seul juge fiscal, sur le combo réellement assemblé.
    """
    if not xbet_odd or xbet_odd <= 1.01 or not sharp_prob or not (0.0 < sharp_prob < 1.0):
        return 0.0, "DISCARD"
    edge = round((sharp_prob * xbet_odd - 1) * 100, 2)
    if edge < min_edge or edge > MAX_EDGE:
        return edge, "DISCARD"
    return edge, "OK"
