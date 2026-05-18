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

# ── Quota-lean sport keys — valid until 2026-06-01 ────────────────────
# Budget: ~150 req remaining + guard stops at 15.
# 15 keys × 3 scans/day = 45/day — guard fires first if budget low.
SPORT_KEYS = {
    # ── North America Playoffs (binary, peak lag) ─────────────────────
    "icehockey_nhl":                     "hockey",      # NHL Conf Finals + Cup Final
    "basketball_nba":                    "basketball",  # NBA Conf Finals
    "baseball_mlb":                      "baseball",    # Regular season
    # ── European Cups (Finals window May 21 / 28 / 31) ───────────────
    "soccer_uefa_champs_league":         "soccer",      # CL Final 2026-05-31
    "soccer_uefa_europa_league":         "soccer",      # EL Final 2026-05-21
    "soccer_uefa_conference_league":     "soccer",      # ECL Final 2026-05-28
    # ── Big-5 Final Matchdays (May 24–25 — all teams simultaneous) ────
    "soccer_spain_la_liga":              "soccer",      # La Liga J38 ~2026-05-24
    "soccer_italy_serie_a":              "soccer",      # Serie A J38 ~2026-05-25
    "soccer_france_ligue_1":             "soccer",      # Ligue 1 J34 ~2026-05-24
    # ── South America (highest lag Pinnacle → 1XBet) ─────────────────
    "soccer_conmebol_copa_libertadores": "soccer",      # Copa Lib R16
    "soccer_brazil_campeonato":          "soccer",      # Brasileirão
    "soccer_argentina_primera_division": "soccer",      # Argentine Primera
    # ── EPL (season finale) ──────────────────────────────────────────
    "soccer_epl":                        "soccer",      # Final matchday ~2026-05-19
    # ── Roland Garros (starts 2026-05-25) ────────────────────────────
    "tennis_atp_french_open":            "tennis",
    "tennis_wta_french_open":            "tennis",
}

# Full sport keys — restore after upgrading OddsAPI plan (need ≥ 30k req/month)
# _SPORT_KEYS_FULL = {49 keys — see git history}

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

def fetch_odds(api_key: str = None, hours_ahead: int = 24,
               sport_keys: dict | None = None) -> list[dict]:
    """
    Fetch events in the next `hours_ahead` hours with h2h + spreads + totals.
    Priority: NBA → Tennis Masters → Soccer.
    sport_keys: override the default SPORT_KEYS dict (used by Golden Hour mode).
    Returns [] if API key missing or quota exhausted (engine falls back to Gemini).
    """
    if not api_key:
        api_key = os.environ.get("ODDS_API_KEY")
    if not api_key:
        log.error("No ODDS_API_KEY — add to .env and GitHub Secrets")
        return []

    keys_to_scan = sport_keys if sport_keys is not None else SPORT_KEYS

    now       = datetime.now(timezone.utc)
    until     = now + timedelta(hours=hours_ahead)
    time_from = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    time_to   = until.strftime("%Y-%m-%dT%H:%M:%SZ")

    all_events = []
    quota_remaining = 9999  # updated after first successful response
    for sport_key, sport_type in keys_to_scan.items():
        if quota_remaining < 15:
            log.warning("OddsAPI quota guard — %d remaining, stopping scan early", quota_remaining)
            break
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
            try:
                quota_remaining = int(remaining)
            except (ValueError, TypeError):
                pass

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
