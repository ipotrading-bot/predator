"""
core/odds_api.py — PAIM v7.5 — Real odds via The Odds API
Replaces 1XBet scraper (harvester.py) + Gemini oracle (oracle.py).
One API call per sport → returns both 1XBet AND Pinnacle odds for the same event.
No hallucinated data. No team matching ambiguity.
"""
import os
import requests

BASE_URL     = "https://api.the-odds-api.com/v4"
PINNACLE_KEY = "pinnacle"
XBET_KEY     = "onexbet"

# Active sport keys → internal sport type
# Tune this list to match what's in season (check /v4/sports for active keys)
SPORT_KEYS = {
    "soccer_epl":                       "soccer",
    "soccer_france_ligue_one":          "soccer",
    "soccer_spain_la_liga":             "soccer",
    "soccer_germany_bundesliga":        "soccer",
    "soccer_italy_serie_a":             "soccer",
    "soccer_uefa_champs_league":        "soccer",
    "soccer_uefa_europa_league":        "soccer",
    "basketball_nba":                   "basketball",
    "tennis_atp_italian_open":          "tennis",
    "tennis_wta_italian_open":          "tennis",
}


def _extract_h2h(bookmakers: list, bookie_key: str, home: str, away: str) -> dict | None:
    """Return {1, X, 2} odds dict for a bookmaker, or None if not found."""
    for bk in bookmakers:
        if bk.get("key") != bookie_key:
            continue
        for mkt in bk.get("markets", []):
            if mkt.get("key") != "h2h":
                continue
            prices = {o["name"]: float(o.get("price", 0)) for o in mkt.get("outcomes", [])}
            return {
                "1": prices.get(home, 0.0),
                "X": prices.get("Draw", 0.0),
                "2": prices.get(away, 0.0),
            }
    return None


def _parse_event(ev: dict, sport_type: str) -> dict | None:
    home = str(ev.get("home_team", "")).strip()
    away = str(ev.get("away_team", "")).strip()
    if not home or not away:
        return None

    bookmakers = ev.get("bookmakers", [])
    xbet     = _extract_h2h(bookmakers, XBET_KEY,     home, away)
    pinnacle = _extract_h2h(bookmakers, PINNACLE_KEY, home, away)

    if not xbet or not pinnacle:
        return None  # Need both bookmakers to compute edge

    return {
        "id":            ev.get("id", f"{home}_{away}"),
        "match":         f"{home} vs {away}",
        "home":          home,
        "away":          away,
        "league":        ev.get("sport_title", ""),
        "sport":         sport_type,
        "commence_time": ev.get("commence_time", ""),
        "odds_1xbet":    xbet,
        "odds_pinnacle": pinnacle,
    }


def fetch_odds(api_key: str = None) -> list[dict]:
    """
    Fetch upcoming events with both 1XBet and Pinnacle h2h odds.
    Logs remaining API credits after each call.
    Returns [] if API key missing or quota exhausted.
    """
    if not api_key:
        api_key = os.environ.get("ODDS_API_KEY")
    if not api_key:
        print("[OddsAPI] No ODDS_API_KEY — set it in .env and GitHub secrets")
        return []

    all_events = []
    for sport_key, sport_type in SPORT_KEYS.items():
        url = f"{BASE_URL}/sports/{sport_key}/odds/"
        params = {
            "apiKey":     api_key,
            "regions":    "eu",
            "markets":    "h2h",
            "bookmakers": f"{PINNACLE_KEY},{XBET_KEY}",
            "oddsFormat": "decimal",
        }
        try:
            r = requests.get(url, params=params, timeout=15)
            remaining = r.headers.get("x-requests-remaining", "?")
            used      = r.headers.get("x-requests-used", "?")

            if r.status_code == 404:
                continue  # Sport not active this season
            if r.status_code in (401, 403):
                print(f"[OddsAPI] Auth error — check ODDS_API_KEY")
                break
            if r.status_code == 422:
                print(f"[OddsAPI] {sport_key}: quota exhausted or bad params")
                break
            if r.status_code != 200:
                print(f"[OddsAPI] {sport_key}: HTTP {r.status_code}")
                continue

            events = [_parse_event(e, sport_type) for e in r.json()]
            events = [e for e in events if e]
            all_events.extend(events)
            print(f"[OddsAPI] {sport_key}: {len(events)} events | used={used} remaining={remaining}")

        except Exception as e:
            print(f"[OddsAPI] {sport_key}: {e}")

    return all_events
