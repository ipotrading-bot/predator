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
}

# Markets fetched per sport (API supports h2h,spreads,totals in one call)
_MARKETS_BY_SPORT = {
    "basketball": "h2h,spreads,totals",
    "hockey":     "h2h,spreads",         # NHL ML + puck line (spreads), no totals needed
    "tennis":     "h2h,totals",          # No spreads market in tennis
    "darts":      "h2h",                 # ML only — no draw in darts
    "cricket":    "h2h",                 # T20 ML only — no draw in T20
    "boxing":     "h2h",                 # ML only — no spreads/totals on boxing
    "soccer":     "h2h,spreads,totals",
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

    xbet_h2h = _extract_h2h(bookmakers, XBET_KEY,     home, away)
    pin_h2h  = _extract_h2h(bookmakers, PINNACLE_KEY, home, away)
    if not xbet_h2h or not pin_h2h:
        return None  # Both books must have h2h for the event to be useful

    event = {
        "id":            ev.get("id", f"{home}_{away}"),
        "match":         f"{home} vs {away}",
        "home":          home,
        "away":          away,
        "league":        ev.get("sport_title", ""),
        "sport":         sport_type,
        "sport_id":      {"soccer": 1, "tennis": 3, "basketball": 4, "boxing": 5, "darts": 6, "cricket": 7, "hockey": 8}.get(sport_type, 1),
        "commence_time": ev.get("commence_time", ""),
        "odds_1xbet":    xbet_h2h,
        "odds_pinnacle": pin_h2h,
    }

    # ── Spreads (NBA + Soccer only) ──────────────────────────────────
    if sport_type not in ("tennis", "boxing", "darts", "cricket"):
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
            "bookmakers":       f"{PINNACLE_KEY},{XBET_KEY}",
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
