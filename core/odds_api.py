"""
core/odds_api.py — PAIM v7.6 — The Odds API (real Pinnacle + 1XBet data)
One call per sport → returns both 1XBet AND Pinnacle odds for the same event.
No hallucinated data. No team matching ambiguity. No rate limit beyond monthly quota.

QUOTA: Free tier = 500 requests/month. Set ODDS_API_KEY in .env + GitHub Secrets.
When quota is exhausted, returns [] and the engine falls back to Gemini sources.
"""
import os
import requests
from datetime import datetime, timedelta, timezone

BASE_URL     = "https://api.the-odds-api.com/v4"
PINNACLE_KEY = "pinnacle"
XBET_KEY     = "onexbet"

# Comprehensive sport keys — 404 = not in season (skipped automatically)
SPORT_KEYS = {
    # Soccer — top European leagues
    "soccer_epl":                        "soccer",
    "soccer_france_ligue_one":           "soccer",
    "soccer_spain_la_liga":              "soccer",
    "soccer_germany_bundesliga":         "soccer",
    "soccer_italy_serie_a":              "soccer",
    "soccer_netherlands_eredivisie":     "soccer",
    "soccer_portugal_primeira_liga":     "soccer",
    "soccer_turkey_super_league":        "soccer",
    # UEFA competitions
    "soccer_uefa_champs_league":         "soccer",
    "soccer_uefa_europa_league":         "soccer",
    "soccer_uefa_conference_league":     "soccer",
    # Basketball
    "basketball_nba":                    "basketball",
    # Tennis — clay season (Roland Garros / Italian Open)
    "tennis_atp_french_open":            "tennis",
    "tennis_wta_french_open":            "tennis",
    "tennis_atp_italian_open":           "tennis",
    "tennis_wta_italian_open":           "tennis",
    "tennis_atp_roland_garros":          "tennis",
    "tennis_wta_roland_garros":          "tennis",
}


def _extract_h2h(bookmakers: list, bookie_key: str, home: str, away: str) -> dict | None:
    """Return {"1", "X", "2"} odds dict for a bookmaker, or None if not found."""
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
        return None

    return {
        "id":            ev.get("id", f"{home}_{away}"),
        "match":         f"{home} vs {away}",
        "home":          home,
        "away":          away,
        "league":        ev.get("sport_title", ""),
        "sport":         sport_type,
        "sport_id":      {"soccer": 1, "tennis": 3, "basketball": 4}.get(sport_type, 1),
        "commence_time": ev.get("commence_time", ""),
        "odds_1xbet":    xbet,
        "odds_pinnacle": pinnacle,
    }


def fetch_odds(api_key: str = None, hours_ahead: int = 24) -> list[dict]:
    """
    Fetch events in the next `hours_ahead` hours with both 1XBet and Pinnacle h2h odds.
    Returns [] if API key missing or quota exhausted (engine falls back to Gemini).
    """
    if not api_key:
        api_key = os.environ.get("ODDS_API_KEY")
    if not api_key:
        print("[OddsAPI] No ODDS_API_KEY — add to .env and GitHub Secrets")
        return []

    now       = datetime.now(timezone.utc)
    until     = now + timedelta(hours=hours_ahead)
    time_from = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    time_to   = until.strftime("%Y-%m-%dT%H:%M:%SZ")

    all_events = []
    for sport_key, sport_type in SPORT_KEYS.items():
        url = f"{BASE_URL}/sports/{sport_key}/odds/"
        params = {
            "apiKey":           api_key,
            "regions":          "eu",
            "markets":          "h2h",
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
                print("[OddsAPI] Auth error — check ODDS_API_KEY")
                return []
            if r.status_code == 422:
                print(f"[OddsAPI] Quota exhausted — falling back to Gemini")
                return []
            if r.status_code != 200:
                print(f"[OddsAPI] {sport_key}: HTTP {r.status_code}")
                continue

            events = [_parse_event(e, sport_type) for e in r.json()]
            events = [e for e in events if e]
            all_events.extend(events)
            if events:
                print(f"[OddsAPI] {sport_key}: {len(events)} events | used={used} remaining={remaining}")

        except Exception as e:
            print(f"[OddsAPI] {sport_key}: {e}")

    return all_events
