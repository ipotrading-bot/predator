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

    La ligue passe par `source_adapter.league_key_correlation` : le libellé
    BRUT dépend de la source qui a rendu le match, donc deux matchs du même
    championnat portaient deux tags. Mesuré le 2026-09-04 — « Ligue 1 - France »
    (OddsAPI) et « FRA D1 » (Tier 2), même journée : la garde les tenait pour
    indépendants. Cette canonisation est volontairement plus large que celle de
    l'appariement des sources ; le pourquoi est dans sa docstring, et il ne se
    devine pas.
    """
    from core.source_adapter import league_key_correlation
    date = (match_time or "")[:10]
    return f"{sport}:{date}:{league_key_correlation(league)}"


# MMA uses the same binary ML logic as boxing/tennis — no draw possible
MMA_SPORTS = {"mma", "boxing"}


def convert_to_ah0(v1: float, vx: float, v2: float) -> tuple[float, float]:
    """(DNB_domicile, DNB_extérieur) DÉVIGORISÉS à partir d'un 1X2 brut.

    ⚠️ CÔTÉ SHARP UNIQUEMENT. Ces deux prix ont eu leur marge retirée : ce
    sont des ESTIMATIONS DE PROBABILITÉ, pas des cotes qu'un book affiche.
    Les employer comme prix d'entrée soft fait mesurer une divergence
    d'opinion et l'appeler « edge » — c'est exactement le défaut corrigé le
    2026-08-27 dans `core.math_engine.to_binary`. Pour un prix d'entrée,
    utiliser `core.math_engine.executable_price` / `synthetic_dnb`.

    Sans appelant dans le dépôt au 2026-08-27 : conservée parce qu'un helper
    de conversion 1X2→AH0 est légitime côté sharp, mais dûment étiquetée pour
    qu'elle ne soit pas ressaisie de bonne foi du mauvais côté.
    """
    return calc_dnb(v1, v2, vx), calc_dnb(v2, v1, vx)


_AGE      = re.compile(r'\bu-?(\d{2})\b')
_RESERVE  = re.compile(r'\b(?:reserves?|res)\b|(?:\bii|\bb)\s*$')
_FEMININ  = re.compile(r'\b(?:women|womens|ladies|feminin\w*|femenino|femminile|frauen|dames)\b'
                       r'|\(w\)|\bw\s*$')


@lru_cache(maxsize=512)
def _niveau(name: str) -> tuple[str, bool]:
    """(catégorie d'âge/équipe, féminines) — l'ÉTAGE d'un club.

    « Sheffield Wednesday Reserve U21 » et « Sheffield Wednesday » se
    ressemblent énormément : `_normalize_team` puis le containment les
    déclarent identiques. Ce sont deux équipes différentes, qui jouent des
    matchs différents. Mesuré le 2026-09-04 : la garde de couverture
    (`core.score_sources.fixture_connue`, un seul nom apparié) laissait un
    U21 hériter de la couverture ESPN de son club senior — le signal était
    émis comme « réglable », puis `result_from_espn` exigeait les deux noms
    et ne le trouvait jamais. Ces lignes restaient `active` jusqu'à
    EXPIRE_AFTER_H en brûlant le budget TheSportsDB à chaque audit.

    Le même piège vaut côté COTES, où il est pire : apparier un U21 au match
    senior lierait le prix d'un match aux cotes d'un autre.

    L'âge prime sur « réserve » (« Reserve U21 » = u21) pour qu'une source
    écrivant « U21 » et une autre « Reserve U21 » restent appariables ; les
    féminines sont un axe séparé, une équipe pouvant être « Women U19 ».
    """
    s = (name or "").lower().strip()
    age = _AGE.search(s)
    return (f"u{age.group(1)}" if age else ("reserve" if _RESERVE.search(s) else ""),
            bool(_FEMININ.search(s)))


@lru_cache(maxsize=512)
def _sans_etage(name: str) -> str:
    """Le nom de club SEUL, marqueurs d'étage retirés. À n'employer qu'après
    avoir comparé les étages : sur « Sheffield Wednesday U21 » il rend
    « sheffield wednesday », c'est-à-dire exactement la confusion contre
    laquelle `_niveau` protège."""
    s = (name or "").lower().strip()
    for motif in (_AGE, _RESERVE, _FEMININ):
        s = motif.sub(" ", s)
    return " ".join(s.split()) or (name or "").lower().strip()


def strict_team_match(name_a: str, name_b: str, threshold: float = 0.60) -> bool:
    """True if both names likely refer to the same team (handles abbreviations).

    Deux noms d'ÉTAGES différents (jeunes, réserve, féminines) sont refusés
    d'emblée, quelle que soit leur ressemblance — voir `_niveau`."""
    if not name_a or not name_b:
        return True
    if _niveau(name_a) != _niveau(name_b):
        return False
    # L'étage a été comparé EXACTEMENT ci-dessus ; on le retire avant de
    # comparer les noms de club, sinon le marqueur casse le containment qui
    # marchait sans lui — « Lyon » ⊂ « Olympique Lyonnais » est vrai, « Lyon
    # U19 » ⊄ « Olympique Lyonnais U19 » ne l'est plus.
    a = _sans_etage(name_a)
    b = _sans_etage(name_b)
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

# `exchange` ajouté le 2026-08-27 (A5). Jusque-là le consensus n'avait, en
# pratique, qu'UNE SEULE source active : `circa` et `cris` ne sont posés que
# par `core/odds_api.py`, obsolète depuis le 2026-08-26, et `isn` n'est écrit
# nulle part dans le dépôt. `calculate_consensus_price` recevait donc
# `{"pinnacle": prix}` et rendait ce prix inchangé — un « consensus » d'un seul
# avis. L'exchange lui redonne un sens.
#
# Le poids importe moins qu'il n'y paraît, et c'est voulu : l'exchange n'entre
# au consensus QUE s'il s'accorde avec Pinnacle à moins de
# `constants.EXCHANGE_DIVERGENCE_PTS` près (run_engine._enrich_from_exchange
# refuse le signal au-delà). Son influence est donc bornée par construction.
# 0.25 le place sous Pinnacle partout : un carnet d'exchange est un prix réel,
# mais sa profondeur varie d'un match à l'autre là où celle de Pinnacle non.
_CONSENSUS_WEIGHTS: dict[str, dict[str, float]] = {
    "basketball": {"pinnacle": 0.30, "circa": 0.50, "cris": 0.10, "isn": 0.10, "exchange": 0.25},
    "euroleague_basketball": {"pinnacle": 0.30, "circa": 0.50, "cris": 0.10, "isn": 0.10, "exchange": 0.25},  # mêmes mécaniques
    "baseball":   {"pinnacle": 0.30, "circa": 0.50, "cris": 0.10, "isn": 0.10, "exchange": 0.25},
    "soccer":     {"pinnacle": 0.40, "circa": 0.10, "cris": 0.20, "isn": 0.30, "exchange": 0.25},
    "tennis":     {"pinnacle": 0.60, "circa": 0.05, "cris": 0.25, "isn": 0.10, "exchange": 0.25},
}
_DEFAULT_WEIGHTS   = {"pinnacle": 0.50, "circa": 0.20, "cris": 0.20, "isn": 0.10, "exchange": 0.25}
_CONSENSUS_SOURCES = ("pinnacle", "circa", "cris", "isn", "exchange")
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
    prices_by_source: {"pinnacle": 2.05, "exchange": 2.07, "circa": 0.0, …}
    Returns (consensus_price, sources_found, is_volatile, consensus_score).
    consensus_score: 0-100 — 100 = perfect agreement, 0 = at CV limit.

    Divergence: Coefficient of Variation (STD/mean) — scale-invariant.
    Prevents false VOLATILE rejection on high-odds underdogs where absolute
    STD of 0.02 is meaningless at odds 3.0+ but was blocking valid signals.
    """
    weights = (_CONSENSUS_WEIGHTS.get(sport) or _DEFAULT_WEIGHTS).copy()
    sources_found: dict[str, bool] = {}
    active: dict[str, float] = {}

    for src in _CONSENSUS_SOURCES:
        price = prices_by_source.get(src, 0.0)
        ok = isinstance(price, (int, float)) and float(price) > 1.01
        sources_found[src] = ok
        if ok and src in weights:
            active[src] = float(price)

    if not active:
        return 0.0, sources_found, False, 0

    # ── Contrôle de divergence — l'exchange en est EXCLU (A5, 2026-08-27) ──
    # `_DIVERGENCE_CV_LIMIT` a été calibré quand le consensus opposait des
    # BOOKMAKERS entre eux (Pinnacle, Circa, Cris), qui cotent à quelques
    # millièmes près. Un prix milieu d'EXCHANGE est structurellement plus
    # dispersé : sa profondeur varie d'un match à l'autre.
    #
    # Mesuré le 2026-08-27 : opposer Pinnacle à Matchbook fait dépasser la
    # limite dès **0,46 point de probabilité** d'écart — c'est-à-dire sur
    # PRESQUE TOUS les matchs. Laisser le CV juger cette paire rendrait la
    # contre-expertise inopérante : tout ce qu'elle accepte serait aussitôt
    # rejeté en VOLATILE, et pour un motif qui nomme mal la cause.
    #
    # La divergence de l'exchange est donc jugée EN AMONT, en POINTS de
    # probabilité, par `run_engine._enrich_from_exchange`
    # (`constants.EXCHANGE_DIVERGENCE_PTS`) — l'unité que ce dépôt impose déjà
    # partout ailleurs (`core/source_adapter.py` : « divergence en POINTS de
    # probabilité, pas en % relatif — un seuil relatif crie au loup sur tout
    # outsider »). Un prix d'exchange qui arrive ici a DÉJÀ passé ce contrôle.
    # Le re-juger dans une seconde unité ne l'améliore pas, il l'annule.
    #
    # Il reste PLEINEMENT compté dans la moyenne pondérée ci-dessous : il est
    # exclu du juge, pas du vote.
    juges = [v for src, v in active.items() if src != "exchange"]

    consensus_score = 100
    if len(juges) >= 2:
        mean = sum(juges) / len(juges)
        # Sample STD (Bessel correction)
        std  = (sum((v - mean) ** 2 for v in juges) / (len(juges) - 1)) ** 0.5
        cv   = std / mean if mean > 0 else 0.0   # Coefficient of Variation
        if cv > _DIVERGENCE_CV_LIMIT:
            return 0.0, sources_found, True, 0
        consensus_score = max(0, round((1 - cv / _DIVERGENCE_CV_LIMIT) * 100))

    # Proportional weight redistribution for absent sources
    total_w = sum(weights[s] for s in active)
    consensus = sum(active[s] * weights[s] / total_w for s in active)
    return round(consensus, 4), sources_found, False, consensus_score


def compute_alpha(
    executable_odd: float,
    sharp_prob: float,
    min_edge: float = MIN_EDGE,
) -> tuple[float, str]:
    """
    Rend (edge_pct, status) où edge_pct est l'ESPÉRANCE DE GAIN VRAIE :
        edge = (sharp_prob × cote_soft_EXÉCUTABLE − 1) × 100
    avec sharp_prob la probabilité dévigorisée (ensemble de
    core.math_engine.devig). status: "OK" dans [min_edge, MAX_EDGE],
    "DISCARD" sinon.

    ⚠️ `executable_odd` DOIT être un prix qu'un book affiche vraiment — la
    sortie de `core.math_engine.to_binary` / `executable_price`. Le paramètre
    s'appelait `xbet_odd` jusqu'au 2026-08-27 et recevait, en football, un DNB
    DÉVIGORISÉ : la marge du book n'était alors jamais soustraite et cette
    fonction rendait une divergence d'opinion sous le nom d'« espérance de
    gain ». Le nom a changé pour que la confusion ne puisse pas se réinstaller
    en silence.

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
    if not executable_odd or executable_odd <= 1.01 or not sharp_prob or not (0.0 < sharp_prob < 1.0):
        return 0.0, "DISCARD"
    edge = round((sharp_prob * executable_odd - 1) * 100, 2)
    if edge < min_edge or edge > MAX_EDGE:
        return edge, "DISCARD"
    return edge, "OK"
