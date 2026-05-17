"""
core/odds_api.py — PAIM v8.0 — The Odds API (Hunter Multi-Sport Mode)
Markets: h2h (NBA + Tennis) | spreads (NBA + Soccer) | totals (all sports)
Priority: basketball_nba → tennis_atp_masters_1000 → Soccer leagues
Returns one event dict per event with all available markets embedded.
"""
import logging
import os
import requests
from datetime import datetime, timedelta, timezone

log = logging.getLogger("PREDATOR.odds_api")

BASE_URL     = "https://api.the-odds-api.com/v4"
PINNACLE_KEY = "pinnacle"
XBET_KEY     = "onexbet"
CIRCA_KEY    = "circa"        # Circa Sports — sharp US book
CRIS_KEY     = "bookmaker"    # Bookmaker.eu — CRIS network

# Priority sports first — scanned before secondary leagues
SPORT_KEYS = {
    # ── PRIORITY ─────────────────────────────────────────────────────
    "icehockey_nhl":                     "hockey",      # NHL Playoffs — binary, sharp Pinnacle market
    "basketball_nba":                    "basketball",  # NBA Playoffs
    "tennis_atp_masters_1000":           "tennis",      # ATP Masters 1000
    # ── Tennis (clay season + lower tiers) ───────────────────────────
    "tennis_atp_french_open":            "tennis",
    "tennis_wta_french_open":            "tennis",
    "tennis_atp_roland_garros":          "tennis",
    "tennis_wta_roland_garros":          "tennis",
    "tennis_atp_italian_open":           "tennis",
    "tennis_wta_italian_open":           "tennis",
    "tennis_atp_500":                    "tennis",      # ATP 500 (Hamburg, Vienna…)
    "tennis_atp_250":                    "tennis",      # ATP 250 — more lag on soft books
    "tennis_wta_500":                    "tennis",      # WTA 500
    "tennis_wta_250":                    "tennis",      # WTA 250
    "tennis_challenger_tour":            "tennis",      # Challenger — highest lag
    # ── Darts (PDC) ──────────────────────────────────────────────────
    "darts_betway_premier_league":       "darts",       # PDC Premier League Darts
    "darts_master":                      "darts",       # Masters / other PDC
    # ── Cricket (T20 only — no draw) ─────────────────────────────────
    "cricket_ipl":                       "cricket",     # IPL T20
    "cricket_international_t20":         "cricket",     # International T20s
    # ── American Football (NFL) ──────────────────────────────────────
    "americanfootball_nfl":              "americanfootball",
    "americanfootball_ncaaf":            "americanfootball",
    # ── Baseball (MLB) ───────────────────────────────────────────────
    "baseball_mlb":                      "baseball",
    # ── Rugby ────────────────────────────────────────────────────────
    "rugbyunion_premiership":            "rugby",
    "rugbyunion_super_rugby":            "rugby",
    "rugbyleague_nrl":                   "rugby",
    # ── Volleyball ───────────────────────────────────────────────────
    "volleyball_womens_world_championship": "volleyball",
    # ── Boxing ───────────────────────────────────────────────────────
    "boxing_boxing":                     "boxing",
    # ── Soccer ───────────────────────────────────────────────────────
    "soccer_fifa_world_cup":             "soccer",      # FIFA WC 2026 — priority over club leagues
    "soccer_uefa_champs_league":         "soccer",
    "soccer_uefa_europa_league":         "soccer",
    "soccer_uefa_conference_league":     "soccer",
    "soccer_epl":                        "soccer",
    "soccer_spain_la_liga":              "soccer",
    "soccer_italy_serie_a":              "soccer",
    "soccer_france_ligue_one":           "soccer",
    "soccer_germany_bundesliga":         "soccer",
    "soccer_netherlands_eredivisie":     "soccer",
    "soccer_portugal_primeira_liga":     "soccer",
    "soccer_turkey_super_league":        "soccer",
    # ── Soccer — Amériques (fort lag Pinnacle → 1XBet) ───────────────
    "soccer_brazil_campeonato":          "soccer",   # Brasileirão Serie A
    "soccer_argentina_primera_division": "soccer",   # Argentine Primera
    "soccer_conmebol_copa_libertadores": "soccer",   # Copa Libertadores
    "soccer_mexico_ligamx":              "soccer",   # Liga MX
    "soccer_usa_mls":                    "soccer",   # MLS
    # ── Soccer — Asie / Océanie ───────────────────────────────────────
    "soccer_korea_kleague1":             "soccer",   # K League 1
    "soccer_japan_j_league":             "soccer",   # J-League
    "soccer_australia_aleague":          "soccer",   # A-League
    # ── Soccer — Autres compétitions européennes ──────────────────────
    "soccer_belgium_first_div":          "soccer",   # Jupiler Pro League
    "soccer_scotland_premiership":       "soccer",   # SPFL Premiership
    "soccer_greece_super_league":        "soccer",   # Super League Greece
    "soccer_czech_republic_liga":        "soccer",   # Czech First League
    "soccer_poland_ekstraklasa":         "soccer",   # Ekstraklasa
    # ── Basketball — Europe ───────────────────────────────────────────
    "basketball_euroleague":             "basketball",  # EuroLeague
}

# Markets fetched per sport (API supports h2h,spreads,totals in one call)
_MARKETS_BY_SPORT = {
    "basketball":       "h2h,spreads,totals",
    "hockey":           "h2h,spreads,totals",  # NHL ML + puck line + O/U
    "americanfootball": "h2h,spreads,totals", # NFL ML + point spread + O/U
    "baseball":         "h2h,totals",         # MLB ML + O/U (no spreads)
    "rugby":            "h2h,spreads,totals",
    "volleyball":       "h2h,totals",
    "tennis":           "h2h,totals",
    "darts":            "h2h",
    "cricket":          "h2h",
    "boxing":           "h2h",
    "soccer":           "h2h,spreads,totals",
}


# ── Extraction helpers ────────────────────────────────────────────────

def _odd(val) -> float:
    try:
        f = float(val)
        return f if f > 1.01 else 0.0
    except (TypeError, ValueError):
        return 0.0


def _extract_h2h(bookmakers: list, bookie_key: str, home: str, away: str) -> dict | None:
    """{"1": float, "X": float, "2": float} — X=0 for binary sports."""
    for bk in bookmakers:
        if bk.get("key") != bookie_key:
            continue
        for mkt in bk.get("markets", []):
            if mkt.get("key") != "h2h":
                continue
            prices = {o["name"]: _odd(o.get("price")) for o in mkt.get("outcomes", [])}
            return {
                "1": prices.get(home, 0.0),
                "X": prices.get("Draw", 0.0),
                "2": prices.get(away, 0.0),
            }
    return None


def _extract_spreads(bookmakers: list, bookie_key: str, home: str, away: str) -> dict | None:
    """{"home": float, "away": float, "point": float} — point is home team's line."""
    for bk in bookmakers:
        if bk.get("key") != bookie_key:
            continue
        for mkt in bk.get("markets", []):
            if mkt.get("key") != "spreads":
                continue
            result: dict = {}
            for o in mkt.get("outcomes", []):
                price = _odd(o.get("price"))
                point = float(o.get("point", 0))
                if o["name"] == home:
                    result["home"]  = price
                    result["point"] = point
                elif o["name"] == away:
                    result["away"]       = price
                    result["away_point"] = point
            if "home" in result and "away" in result and result["home"] > 1.01:
                return result
    return None


def _extract_totals(bookmakers: list, bookie_key: str) -> dict | None:
    """{"over": float, "under": float, "point": float}."""
    for bk in bookmakers:
        if bk.get("key") != bookie_key:
            continue
        for mkt in bk.get("markets", []):
            if mkt.get("key") != "totals":
                continue
            result: dict = {}
            for o in mkt.get("outcomes", []):
                price = _odd(o.get("price"))
                side  = o.get("name", "").lower()
                if side == "over":
                    result["over"]  = price
                    result["point"] = float(o.get("point", 0))
                elif side == "under":
                    result["under"] = price
            if "over" in result and "under" in result:
                return result
    return None


# ── Event parser ──────────────────────────────────────────────────────

def _parse_event(ev: dict, sport_type: str) -> dict | None:
    home = str(ev.get("home_team", "")).strip()
    away = str(ev.get("away_team", "")).strip()
    if not home or not away:
        return None

    bookmakers = ev.get("bookmakers", [])

    xbet_h2h  = _extract_h2h(bookmakers, XBET_KEY,     home, away)
    pin_h2h   = _extract_h2h(bookmakers, PINNACLE_KEY, home, away)
    if not xbet_h2h or not pin_h2h:
        return None  # Both books must have h2h for the event to be useful

    circa_h2h = _extract_h2h(bookmakers, CIRCA_KEY, home, away)
    cris_h2h  = _extract_h2h(bookmakers, CRIS_KEY,  home, away)

    event = {
        "id":            ev.get("id", f"{home}_{away}"),
        "match":         f"{home} vs {away}",
        "home":          home,
        "away":          away,
        "league":        ev.get("sport_title", ""),
        "sport":         sport_type,
        "sport_id":      {"soccer": 1, "tennis": 3, "basketball": 4, "boxing": 5, "darts": 6, "cricket": 7, "hockey": 8, "americanfootball": 10, "baseball": 11, "rugby": 12, "volleyball": 13}.get(sport_type, 1),
        "commence_time": ev.get("commence_time", ""),
        "odds_1xbet":    xbet_h2h,
        "odds_pinnacle": pin_h2h,
    }
    if circa_h2h:
        event["odds_circa"] = circa_h2h
    if cris_h2h:
        event["odds_cris"] = cris_h2h

    # ── Spreads (binary sports only — tennis/boxing/darts/cricket/baseball have no spreads) ──
    if sport_type not in ("tennis", "boxing", "darts", "cricket", "baseball", "volleyball"):
        xs = _extract_spreads(bookmakers, XBET_KEY,     home, away)
        ps = _extract_spreads(bookmakers, PINNACLE_KEY, home, away)
        if xs and ps:
            event["spreads_1xbet"]    = xs
            event["spreads_pinnacle"] = ps

    # ── Totals (all sports) ───────────────────────────────────────────
    xt = _extract_totals(bookmakers, XBET_KEY)
    pt = _extract_totals(bookmakers, PINNACLE_KEY)
    if xt and pt:
        event["totals_1xbet"]    = xt
        event["totals_pinnacle"] = pt

    return event


# ── Public API ────────────────────────────────────────────────────────

def fetch_odds(api_key: str = None, hours_ahead: int = 24) -> list[dict]:
    """
    Fetch events in the next `hours_ahead` hours with h2h + spreads + totals.
    Priority: NBA → Tennis Masters → Soccer.
    Returns [] if API key missing or quota exhausted (engine falls back to Gemini).
    """
    if not api_key:
        api_key = os.environ.get("ODDS_API_KEY")
    if not api_key:
        log.error("No ODDS_API_KEY — add to .env and GitHub Secrets")
        return []

    now       = datetime.now(timezone.utc)
    until     = now + timedelta(hours=hours_ahead)
    time_from = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    time_to   = until.strftime("%Y-%m-%dT%H:%M:%SZ")

    all_events = []
    for sport_key, sport_type in SPORT_KEYS.items():
        markets = _MARKETS_BY_SPORT.get(sport_type, "h2h")
        url = f"{BASE_URL}/sports/{sport_key}/odds/"
        params = {
            "apiKey":           api_key,
            "regions":          "eu",
            "markets":          markets,
            "bookmakers":       f"{PINNACLE_KEY},{XBET_KEY},{CIRCA_KEY},{CRIS_KEY}",
            "oddsFormat":       "decimal",
            "commenceTimeFrom": time_from,
            "commenceTimeTo":   time_to,
        }
        try:
            r = requests.get(url, params=params, timeout=15)
            remaining = r.headers.get("x-requests-remaining", "?")
            used      = r.headers.get("x-requests-used", "?")

            if r.status_code == 404:
                continue  # Not in season
            if r.status_code in (401, 403):
                log.error("Auth error — check ODDS_API_KEY")
                return []
            if r.status_code == 422:
                log.warning("Quota exhausted — falling back to Gemini")
                return []
            if r.status_code != 200:
                log.warning("%s: HTTP %d", sport_key, r.status_code)
                continue

            events = [_parse_event(e, sport_type) for e in r.json()]
            events = [e for e in events if e]
            all_events.extend(events)
            if events:
                has_totals  = sum(1 for e in events if "totals_1xbet"  in e)
                has_spreads = sum(1 for e in events if "spreads_1xbet" in e)
                log.info("%s: %d events | totals=%d spreads=%d | used=%s remaining=%s",
                         sport_key, len(events), has_totals, has_spreads, used, remaining)

        except Exception as e:
            log.error("%s: %s", sport_key, e)

    return all_events
