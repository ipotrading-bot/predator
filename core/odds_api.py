"""
core/odds_api.py — PAIM v8.3 — The Odds API (Hunter Multi-Sport Mode)
Markets: h2h | spreads | totals selon le sport

Budget 20 000 req/mois — 30 sport keys actifs — Équation :
  7×G + 30×(E+D) = 666,67 req/jour  →  G=48, E=8, D=3
  GH  : 48×7  = 336/j → 10 080/mois
  Eng :  8×30 = 240/j →  7 200/mois
  Deep:  3×30 =  90/j →  2 700/mois
  TOTAL         666/j → 19 980/mois (99,9 %)
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

# ── Sport keys actifs — sélection RENTABILITÉ MAXIMALE ───────────────
# Critères de sélection :
#   1. Lag Pinnacle→Melbet documenté (source d'edge réel)
#   2. Kelly fraction élevée (≥ 0.18 — confiance marché)
#   3. Volume quotidien suffisant (≥ 3 matchs/jour en moyenne)
#   4. Données Pinnacle + Melbet confirmées disponibles
#
# EXCLUS (signal/bruit trop faible) :
#   - Cricket / Darts / Boxing (Kelly 0.10–0.15, marchés peu efficients)
#   - Ligues Scandi / Irlande / Chine / Japon / Corée soccer (lag faible, volumes bas)
#   - Copa Sudamericana / Brazil B / Chile / Colombia / Argentina (Pinnacle peu liquide)
#   - Tennis (saison de transition gazon — incertitude surface/forme)
#   - Liga MX (structures cotes non fiables sur Melbet)
#
# Budget : Engine 6×/j × 11 = 66/j | Deep 2×/j × 11 = 22/j | Total : ~2 640/mois.
SPORT_KEYS = {
    # ── PRIORITÉ 1 — FIFA World Cup 2026 (lag maximal garanti) ────────
    "soccer_fifa_world_cup":                 "soccer",      # 48 matchs phase groupes — lag +2h documenté

    # ── PRIORITÉ 2 — Playoffs Amérique du Nord (sharps = Kelly 0.25–0.30) ──
    "basketball_nba":                        "basketball",  # NBA Finals — marché le + sharp au monde
    "icehockey_nhl":                         "hockey",      # NHL Stanley Cup Finals — mouvement max
    "baseball_mlb":                          "baseball",    # MLB — 10+ matchs/jour, lag US→EU ✓

    # ── PRIORITÉ 3 — Baseball Asie (lag timezone = fenêtre AM UTC) ────
    "baseball_kbo":                          "baseball",    # KBO Corée — lag Asie 06:00–13:00 UTC ✓
    "baseball_npb":                          "baseball",    # NPB Japon — lag Asie 06:00–13:00 UTC ✓

    # ── PRIORITÉ 4 — Matchs amicaux internationaux (fenêtre WC pré-tournoi) ─
    "soccer_international_friendlies":       "soccer",      # Amicaux équipes nationales — actifs mai–juin 2026

    # ── PRIORITÉ 5 — Copa Libertadores (lag SA soirée = fenêtre 21:00 UTC) ─
    "soccer_conmebol_copa_libertadores":     "soccer",      # R16/QF — lag SA maximal documenté

    # ── PRIORITÉ 6 — Brasileirão (quotidien, lag BR→EU cohérent) ─────
    "soccer_brazil_campeonato":              "soccer",      # Série A Brésil — marché sharp actif

    # ── PRIORITÉ 7 — MLS (très actif juin–août, lag NA→EU) ───────────
    "soccer_usa_mls":                        "soccer",      # MLS — volumes élevés, 1XBet actif

    # ── PRIORITÉ 7 — Australie (marchés Pinnacle très sharps) ────────
    "aussierules_afl":                       "aussierules", # AFL — ~9 matchs/semaine, Pinnacle ✓
    "rugby_nrl":                             "rugbyleague", # NRL — ~8 matchs/semaine, Pinnacle ✓
}

# Markets fetched per sport (API supports h2h,spreads,totals in one call)
_MARKETS_BY_SPORT = {
    "basketball":       "h2h,spreads,totals",
    "hockey":           "h2h,spreads,totals",  # NHL ML + puck line + O/U
    "americanfootball": "h2h,spreads,totals",  # NFL ML + point spread + O/U
    "baseball":         "h2h,totals",          # MLB ML + O/U (no spreads)
    "rugby":            "h2h,spreads,totals",
    "rugbyleague":      "h2h,spreads,totals",  # NRL — même structure que rugby union
    "aussierules":      "h2h,spreads,totals",  # AFL — ligne = 6.5+ pts typique
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
        "sport_id":      {"soccer": 1, "tennis": 3, "basketball": 4, "boxing": 5, "darts": 6, "cricket": 7, "hockey": 8, "americanfootball": 10, "baseball": 11, "rugby": 12, "volleyball": 13, "tabletennis": 14, "handball": 15, "aussierules": 16, "rugbyleague": 17}.get(sport_type, 1),
        "commence_time": ev.get("commence_time", ""),
        "odds_1xbet":    xbet_h2h,
        "odds_pinnacle": pin_h2h,
    }
    if circa_h2h:
        event["odds_circa"] = circa_h2h
    if cris_h2h:
        event["odds_cris"] = cris_h2h

    # ── Spreads (binary sports only — tennis/boxing/darts/cricket/baseball have no spreads) ──
    if sport_type not in ("tennis", "boxing", "darts", "cricket", "baseball", "volleyball", "rugbyleague"):
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

def fetch_odds(api_key: str | None = None, hours_ahead: int = 24,
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
    assert api_key is not None  # narrow type after early return

    keys_to_scan = sport_keys if sport_keys is not None else SPORT_KEYS

    now       = datetime.now(timezone.utc)
    until     = now + timedelta(hours=hours_ahead)
    time_from = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    time_to   = until.strftime("%Y-%m-%dT%H:%M:%SZ")

    all_events = []
    quota_remaining = 9999  # updated after first successful response
    for sport_key, sport_type in keys_to_scan.items():
        if quota_remaining < 50:
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
