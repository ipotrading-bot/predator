"""
data/multi_source_fetcher.py — PREDATOR Multi-Source Intelligence v1.0

Sources (sans quota The-Odds-API) :
  1. Gemini Market Scout     — 1 requête Google Search → top opps avec cotes
  2. API-Sports.io (Soccer)  — api_football_key → fixtures 48h
  3. API-Sports.io (Basket)  — api_football_key → NBA games
  4. TheSportsDB             — gratuit, aucune clé → fixtures multisports
  5. Gemini Enricher         — par match sans cotes → Gemini Google Search
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests as req

from config import settings

logger = logging.getLogger(__name__)

# ── Constantes ────────────────────────────────────────────────────
TSDB_BASE   = "https://www.thesportsdb.com/api/v1/json/3"
AF_BASE     = "https://v3.football.api-sports.io"
BBALL_BASE  = "https://v1.basketball.api-sports.io"
GEMINI_URL  = (
    "https://generativelanguage.googleapis.com/v1beta/models"
    "/gemini-2.0-flash-exp:generateContent"
)

# TheSportsDB league id → notre sport_key
TSDB_LEAGUES: list[tuple[str, str]] = [
    ("4328", "soccer_epl"),
    ("4332", "soccer_spain_la_liga"),
    ("4335", "soccer_germany_bundesliga"),
    ("4331", "soccer_italy_serie_a"),
    ("4334", "soccer_france_ligue_1"),
    ("4480", "soccer_uefa_champs_league"),
    ("4399", "basketball_nba"),
    ("4387", "baseball_mlb"),
    ("4380", "icehockey_nhl"),
]

# API-Football league id → sport_key
AF_LEAGUES: dict[int, str] = {
    39:  "soccer_epl",
    140: "soccer_spain_la_liga",
    135: "soccer_italy_serie_a",
    78:  "soccer_germany_bundesliga",
    61:  "soccer_france_ligue_1",
    2:   "soccer_uefa_champs_league",
    3:   "soccer_uefa_europa_league",
    848: "soccer_uefa_europa_league",
}


def _eid(source: str, home: str, away: str, date: str) -> str:
    slug = f"{source}_{home}_{away}_{date}".lower().replace(" ", "_")
    return re.sub(r"[^a-z0-9_]", "", slug)[:80]


def _parse_iso(s: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _in_window(ts: str, hours: int = 48) -> bool:
    t = _parse_iso(ts)
    if not t:
        return False
    now = datetime.now(timezone.utc)
    return now <= t <= now + timedelta(hours=hours)


# ═══════════════════════════════════════════════════════════════════
# SOURCE 1 — TheSportsDB (free, no key)
# ═══════════════════════════════════════════════════════════════════

class TheSportsDBClient:
    """Fixtures gratuits pour 9 grandes ligues."""

    def get_upcoming_events(self, hours: int = 48) -> list[dict]:
        events: list[dict] = []
        now    = datetime.now(timezone.utc)
        cutoff = now + timedelta(hours=hours)

        for league_id, sport_key in TSDB_LEAGUES:
            try:
                resp = req.get(
                    f"{TSDB_BASE}/eventsnextleague.php",
                    params={"id": league_id},
                    timeout=8,
                )
                if resp.status_code != 200:
                    continue

                for ev in (resp.json().get("events") or []):
                    date_part = ev.get("dateEvent", "")
                    time_part = ev.get("strTime") or "00:00:00"
                    ts = f"{date_part}T{time_part}Z"
                    t  = _parse_iso(ts)
                    if not t or t < now or t > cutoff:
                        continue

                    home = ev.get("strHomeTeam", "").strip()
                    away = ev.get("strAwayTeam", "").strip()
                    if not home or not away:
                        continue

                    events.append({
                        "id":           _eid("tsdb", home, away, date_part),
                        "sport_key":    sport_key,
                        "home_team":    home,
                        "away_team":    away,
                        "commence_time": t.isoformat(),
                        "bookmakers":   [],
                        "_source":      "thesportsdb",
                    })

                time.sleep(0.25)

            except Exception as e:
                logger.debug(f"TSDB {league_id}: {e}")

        logger.info(f"[TheSportsDB] {len(events)} events")
        return events


# ═══════════════════════════════════════════════════════════════════
# SOURCE 2 — API-Sports.io (api_football_key)
# ═══════════════════════════════════════════════════════════════════

class ApiSportsClient:
    """Fixtures Soccer + NBA via api-sports.io (100 req/jour gratuit)."""

    def __init__(self):
        self.key  = settings.api_football_key or settings.rapidapi_key
        self.used = 0

    def _get(self, base: str, endpoint: str, params: dict) -> dict:
        if not self.key:
            return {}
        try:
            r = req.get(
                f"{base}{endpoint}",
                headers={"x-apisports-key": self.key, "x-rapidapi-key": self.key},
                params=params,
                timeout=10,
            )
            self.used += 1
            if r.status_code == 200:
                return r.json()
        except Exception as e:
            logger.debug(f"ApiSports {endpoint}: {e}")
        return {}

    def get_soccer_fixtures(self, hours: int = 48) -> list[dict]:
        if not self.key:
            return []
        now    = datetime.now(timezone.utc)
        cutoff = now + timedelta(hours=hours)
        d_from = now.strftime("%Y-%m-%d")
        d_to   = cutoff.strftime("%Y-%m-%d")

        data  = self._get(AF_BASE, "/fixtures", {
            "from": d_from, "to": d_to,
            "timezone": "UTC", "status": "NS",
        })
        events: list[dict] = []

        for fix in data.get("response", []):
            try:
                f      = fix.get("fixture", {})
                teams  = fix.get("teams", {})
                league = fix.get("league", {})
                lid    = league.get("id", 0)
                sport  = AF_LEAGUES.get(lid, f"soccer_{league.get('country','unk').lower()}")
                ts     = f.get("date", "")
                t      = _parse_iso(ts)
                if not t or t < now or t > cutoff:
                    continue
                home = teams.get("home", {}).get("name", "").strip()
                away = teams.get("away", {}).get("name", "").strip()
                if not home or not away:
                    continue
                events.append({
                    "id":            f"afsports_{f.get('id', _eid('af', home, away, ts[:10]))}",
                    "sport_key":     sport,
                    "home_team":     home,
                    "away_team":     away,
                    "commence_time": t.isoformat(),
                    "bookmakers":    [],
                    "_source":       "api_football",
                })
            except Exception:
                pass

        logger.info(f"[API-Sports Soccer] {len(events)} fixtures")
        return events

    def get_nba_games(self, hours: int = 48) -> list[dict]:
        if not self.key:
            return []
        events: list[dict] = []
        now    = datetime.now(timezone.utc)
        cutoff = now + timedelta(hours=hours)

        for delta in range(2):
            d = (now + timedelta(days=delta)).strftime("%Y-%m-%d")
            data = self._get(BBALL_BASE, "/games", {
                "date": d, "league": "12", "timezone": "UTC",
            })
            for g in data.get("response", []):
                try:
                    status = g.get("status", {}).get("short", "")
                    if status not in ("NS", ""):
                        continue
                    ts = g.get("date", "")
                    t  = _parse_iso(ts)
                    if not t or t < now or t > cutoff:
                        continue
                    home = g.get("teams", {}).get("home", {}).get("name", "").strip()
                    away = g.get("teams", {}).get("visitors", {}).get("name", "").strip()
                    if not home or not away:
                        continue
                    events.append({
                        "id":            f"nba_{g.get('id', _eid('nba', home, away, d))}",
                        "sport_key":     "basketball_nba",
                        "home_team":     home,
                        "away_team":     away,
                        "commence_time": t.isoformat(),
                        "bookmakers":    [],
                        "_source":       "api_sports_nba",
                    })
                except Exception:
                    pass

        logger.info(f"[API-Sports NBA] {len(events)} games")
        return events


# ═══════════════════════════════════════════════════════════════════
# SOURCE 3 — Gemini Market Scout (1 appel → top opps avec cotes)
# ═══════════════════════════════════════════════════════════════════

_SCOUT_PROMPT = """\
Tu es un agent d'intelligence de marché sportif niveau PhD MIT.

DATE AUJOURD'HUI : {date}

MISSION : Utilise Google Search pour trouver les 15 meilleurs matchs des prochaines \
48h avec des ÉCARTS de cotes entre bookmakers — ces écarts représentent de la valeur (EV+).

Cherche spécifiquement :
- matchs NBA ce soir et demain + cotes Pinnacle / Bet365
- matchs soccer (EPL, Bundesliga, Champions League) + cotes
- UFC / MMA cette semaine
- Tennis ATP/WTA en cours

Pour chaque opportunité, retourne un JSON STRICT (pas de texte autour) :
{{
  "opportunities": [
    {{
      "home_team": "Boston Celtics",
      "away_team": "Miami Heat",
      "sport_key": "basketball_nba",
      "commence_time": "2026-05-11T23:30:00Z",
      "pinnacle": {{"home": 1.65, "away": 2.35}},
      "bet365":   {{"home": 1.72, "away": 2.20}},
      "williamhill": {{"home": 1.70, "away": 2.25}},
      "unibet":   {{"home": 1.71, "away": 2.22}},
      "onexbet":  {{"home": 1.75, "away": 2.18}},
      "edge_note": "Home undervalued chez 1XBet vs Pinnacle"
    }}
  ],
  "scan_date": "{date}"
}}

Règles :
- Cotes DÉCIMALES uniquement (ex: 1.85 et non +185)
- Soccer : inclure "draw" dans chaque bookmaker
- Basketbal/Tennis : seulement "home" et "away"
- N'invente JAMAIS des cotes — si introuvable, omets le bookmaker
- Seulement les matchs dans les 48h à venir (après {date})
"""

_ENRICH_PROMPT = """\
Tu es un agent d'extraction de cotes sportives.

Recherche sur Google les cotes EN DIRECT pour ce match :
Match : {home} vs {away}
Sport : {sport}
Date  : {date}

Trouve les cotes de : Pinnacle, Bet365, William Hill, Unibet, 1XBet.

Retourne UNIQUEMENT ce JSON (zéro texte autour) :
{{
  "found": true,
  "pinnacle":    {{"home": 1.85, "draw": 3.40, "away": 2.10}},
  "bet365":      {{"home": 1.90, "draw": 3.30, "away": 2.00}},
  "williamhill": {{"home": 1.88, "draw": 3.35, "away": 2.05}},
  "unibet":      {{"home": 1.87, "draw": 3.38, "away": 2.08}},
  "onexbet":     {{"home": 1.92, "draw": 3.25, "away": 2.02}}
}}

Si match introuvable : {{"found": false}}
Format décimal uniquement. Soccer = 3 outcomes, Basket/Tennis = 2 outcomes.
"""


def _gemini_call(prompt: str, api_key: str, max_tokens: int = 2000) -> str:
    """Appel Gemini 2.0 Flash avec Search Grounding. Retourne le texte brut."""
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "tools": [{"google_search": {}}],
        "generationConfig": {"temperature": 0.15, "maxOutputTokens": max_tokens},
    }
    try:
        r = req.post(f"{GEMINI_URL}?key={api_key}", json=payload, timeout=40)
        if r.status_code != 200:
            logger.debug(f"Gemini {r.status_code}: {r.text[:120]}")
            return ""
        candidates = r.json().get("candidates", [])
        if not candidates:
            return ""
        return "".join(
            p.get("text", "")
            for p in candidates[0].get("content", {}).get("parts", [])
        )
    except Exception as e:
        logger.warning(f"Gemini call error: {e}")
        return ""


def _extract_json(text: str) -> Optional[dict]:
    """Extrait le premier bloc JSON valide du texte."""
    for pattern in (r'\{[\s\S]*\}',):
        m = re.search(pattern, text)
        if m:
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                pass
    return None


def _build_bookmakers(odds: dict, home: str, away: str, is_soccer: bool) -> list[dict]:
    """Convertit un dict {bm_key: {home, away, draw}} vers format The-Odds-API."""
    BM_KEYS = ["pinnacle", "bet365", "williamhill", "unibet", "onexbet", "melbet", "bwin"]
    bookmakers: list[dict] = []

    for bm in BM_KEYS:
        bm_odds = odds.get(bm)
        if not isinstance(bm_odds, dict):
            continue
        outcomes: list[dict] = []
        h = bm_odds.get("home")
        a = bm_odds.get("away")
        d = bm_odds.get("draw")
        if h and float(h) > 1.01:
            outcomes.append({"name": home, "price": float(h)})
        if a and float(a) > 1.01:
            outcomes.append({"name": away, "price": float(a)})
        if is_soccer and d and float(d) > 1.01:
            outcomes.append({"name": "Draw", "price": float(d)})
        if len(outcomes) >= 2:
            bookmakers.append({"key": bm, "markets": [{"key": "h2h", "outcomes": outcomes}]})

    return bookmakers


class GeminiMarketScout:
    """Un seul appel Gemini → liste d'opportunités avec cotes."""

    def __init__(self):
        self.key = settings.gemini_api_key
        self.calls = 0

    def scout(self) -> list[dict]:
        if not self.key:
            return []

        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        text = _gemini_call(_SCOUT_PROMPT.format(date=date), self.key, max_tokens=3000)
        self.calls += 1

        data = _extract_json(text)
        if not data or "opportunities" not in data:
            logger.warning("[GeminiScout] Pas de JSON valide dans la réponse")
            return []

        events: list[dict] = []
        now    = datetime.now(timezone.utc)
        cutoff = now + timedelta(hours=48)

        for opp in data.get("opportunities", []):
            home    = opp.get("home_team", "").strip()
            away    = opp.get("away_team", "").strip()
            sport   = opp.get("sport_key", "unknown")
            ts      = opp.get("commence_time", "")
            if not home or not away or not ts:
                continue
            t = _parse_iso(ts)
            if not t or t < now or t > cutoff:
                continue

            is_soccer = "soccer" in sport
            bookmakers = _build_bookmakers(opp, home, away, is_soccer)
            if not bookmakers:
                continue

            events.append({
                "id":            _eid("scout", home, away, ts[:10]),
                "sport_key":     sport,
                "home_team":     home,
                "away_team":     away,
                "commence_time": ts,
                "bookmakers":    bookmakers,
                "_source":       "gemini_scout",
                "_edge":         opp.get("edge_note", ""),
            })

        logger.info(f"[GeminiScout] {len(events)} opportunities")
        return events


class GeminiOddsEnricher:
    """Enrichit les fixtures sans cotes via Gemini (max_calls par run)."""

    def __init__(self):
        self.key   = settings.gemini_api_key
        self.calls = 0

    def enrich(self, events: list[dict], max_calls: int = 15) -> list[dict]:
        if not self.key:
            return []

        enriched: list[dict] = []
        for ev in events[:max_calls]:
            home  = ev.get("home_team", "")
            away  = ev.get("away_team", "")
            sport = ev.get("sport_key", "")
            date  = (ev.get("commence_time") or "")[:10]

            if not home or not away:
                continue

            prompt = _ENRICH_PROMPT.format(
                home=home, away=away, sport=sport, date=date,
            )
            text = _gemini_call(prompt, self.key, max_tokens=600)
            self.calls += 1
            time.sleep(1.1)  # rate-limit doux

            data = _extract_json(text)
            if not data or not data.get("found"):
                continue

            is_soccer  = "soccer" in sport
            bookmakers = _build_bookmakers(data, home, away, is_soccer)
            if not bookmakers:
                continue

            ev["bookmakers"]    = bookmakers
            ev["_odds_source"]  = "gemini_enrich"
            enriched.append(ev)
            logger.info(f"[GeminiEnrich] {home} vs {away} → {len(bookmakers)} books")

        return enriched


# ═══════════════════════════════════════════════════════════════════
# ORCHESTRATEUR — MultiSourceFetcher
# ═══════════════════════════════════════════════════════════════════

class MultiSourceFetcher:
    """
    Agrège toutes les sources alternatives et retourne des events
    au format The-Odds-API, prêts pour le scanner PAIM.

    Ordre de priorité :
    1. Gemini Market Scout (1 appel → top opps avec cotes)
    2. API-Sports soccer + NBA → enrichis par Gemini si besoin
    3. TheSportsDB → enrichis par Gemini pour les matchs manquants
    """

    MAX_GEMINI_CALLS = 30  # Budget Gemini total par run

    def __init__(self):
        self.scout    = GeminiMarketScout()
        self.enricher = GeminiOddsEnricher()
        self.apisports = ApiSportsClient()
        self.tsdb      = TheSportsDBClient()
        self._stats    = {}

    def fetch_all(self) -> list[dict]:
        all_events: list[dict] = []
        seen: set[str] = set()

        def add(evs: list[dict]):
            for e in evs:
                eid = e.get("id", "")
                if eid and eid not in seen:
                    seen.add(eid)
                    all_events.append(e)

        gemini_budget = self.MAX_GEMINI_CALLS

        # ── Étape 1 : Gemini Market Scout ─────────────────────────
        logger.info("🔍 [MultiSource] Étape 1 : Gemini Market Scout...")
        scout_events = self.scout.scout()
        gemini_budget -= self.scout.calls
        add(scout_events)

        # ── Étape 2 : API-Sports fixtures ─────────────────────────
        logger.info("🏟️ [MultiSource] Étape 2 : API-Sports fixtures...")
        soccer_fixtures = self.apisports.get_soccer_fixtures()
        nba_games       = self.apisports.get_nba_games()
        api_fixtures    = soccer_fixtures + nba_games

        # Filtrer ceux déjà couverts par le Scout (même match)
        new_fixtures = [
            f for f in api_fixtures
            if not any(
                f["home_team"] in e["home_team"] or f["away_team"] in e["away_team"]
                for e in all_events
            )
        ]

        if new_fixtures and gemini_budget > 2:
            enrich_budget = min(gemini_budget - 1, 12)
            enriched = self.enricher.enrich(new_fixtures, max_calls=enrich_budget)
            gemini_budget -= self.enricher.calls
            add(enriched)

        # ── Étape 3 : TheSportsDB ─────────────────────────────────
        logger.info("📊 [MultiSource] Étape 3 : TheSportsDB...")
        tsdb_events = self.tsdb.get_upcoming_events()
        new_tsdb    = [
            e for e in tsdb_events
            if e.get("id") not in seen and not any(
                e["home_team"] in x["home_team"]
                for x in all_events
            )
        ]

        if new_tsdb and gemini_budget > 2:
            enrich_budget = min(gemini_budget - 1, 8)
            enriched_tsdb = self.enricher.enrich(new_tsdb, max_calls=enrich_budget)
            add(enriched_tsdb)

        self._stats = {
            "total_events":    len(all_events),
            "gemini_calls":    self.scout.calls + self.enricher.calls,
            "api_sports_calls": self.apisports.used,
            "scout_events":    len(scout_events),
        }

        logger.info(
            f"✅ [MultiSource] {len(all_events)} events | "
            f"{self._stats['gemini_calls']} Gemini calls | "
            f"{self.apisports.used} API-Sports calls"
        )
        return all_events

    def get_stats(self) -> dict:
        return self._stats
