"""
data/multi_source_fetcher.py — PREDATOR Multi-Source Intelligence v2.0
DOCTRINE "Frugalité de Données" (PhD MIT)

Architecture en cascade — chaque source n'est appelée que si la précédente est vide :
  Tier 1  → The-Odds-API (quota 500/mois — prioritaire, déjà dans odds_fetcher.py)
  Tier 2  → Gemini Market Scout (1 appel → top 15 opportunités avec cotes réelles)
  Tier 3  → API-Sports.io (Soccer + NBA via clé RapidAPI — 100 req/jour gratuits)
  Tier 4  → TheSportsDB (gratuit, sans clé — fixtures uniquement, enrichi par Gemini)
  Tier 5  → Gemini Odds Enricher (par match, max 15 appels/run — dernier recours)

INSTRUCTIONS RAPIDAPI (Le Hub Central) :
  Un seul compte RapidAPI donne accès à :
  - API-Football    → soccer (100 req/jour gratuit)
  - NBA API         → basketball_nba
  - Tennis API      → tennis ATP/WTA
  Clé : settings.api_football_key OU settings.rapidapi_key
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timezone, timedelta
from typing import Optional
from urllib.parse import quote

import httpx

from config import settings

logger = logging.getLogger(__name__)

# ── Constantes ────────────────────────────────────────────────────
TSDB_BASE     = "https://www.thesportsdb.com/api/v1/json/3"
AF_BASE       = "https://v3.football.api-sports.io"
BBALL_BASE    = "https://v1.basketball.api-sports.io"
TENNIS_BASE   = "https://v1.tennis.api-sports.io"
GEMINI_URL_FMT = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "{model}:generateContent"
)

GEMINI_SCOUT_MODEL  = "gemini-2.0-flash"
GEMINI_ENRICH_MODEL = "gemini-2.0-flash"

# ── Mapping TheSportsDB league_id → sport_key ─────────────────────
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

# ── Mapping API-Football league_id → sport_key ────────────────────
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

# ── API-Sports league IDs ─────────────────────────────────────────
NBA_LEAGUE_ID  = "12"
TENNIS_ATP_ID  = "atp"
TENNIS_WTA_ID  = "wta"


# ═══════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════

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
# TIER 2 — Gemini Market Scout (1 appel → top 15 opportunités)
# ═══════════════════════════════════════════════════════════════════

_SCOUT_PROMPT = """\
Tu es un agent d'intelligence de marché sportif niveau PhD MIT.

DATE AUJOURD'HUI : {date}

MISSION : Utilise Google Search pour trouver les 15 meilleurs matchs des prochaines \
48h avec des ÉCARTS de cotes entre bookmakers — ces écarts représentent de la valeur (EV+).

Cherche spécifiquement :
- matchs NBA ce soir et demain + cotes Pinnacle / Bet365
- matchs soccer (EPL, Bundesliga, Champions League) + cotes
- Tennis ATP/WTA en cours
- Esports LoL / CS:GO

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
      "onexbet":  {{"home": 1.75, "away": 2.18}},
      "edge_note": "Home undervalued chez 1XBet vs Pinnacle"
    }}
  ],
  "scan_date": "{date}"
}}

Règles :
- Cotes DÉCIMALES uniquement (ex: 1.85 et non +185)
- Soccer : inclure "draw" dans chaque bookmaker
- Basketball/Tennis : seulement "home" et "away"
- N'invente JAMAIS des cotes — si introuvable, omets le bookmaker
- Seulement les matchs dans les 48h à venir
"""


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


def _gemini_call_sync(prompt: str, api_key: str, model: str = GEMINI_SCOUT_MODEL, max_tokens: int = 2000) -> str:
    """
    Appel Gemini synchrone avec Search Grounding.
    Utilisé car le SDK google-generativeai gère mal l'async.
    """
    import requests as _req

    url = GEMINI_URL_FMT.format(model=model)
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "tools": [{"google_search": {}}],
        "generationConfig": {"temperature": 0.15, "maxOutputTokens": max_tokens},
    }
    try:
        r = _req.post(f"{url}?key={api_key}", json=payload, timeout=40)
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


# ═══════════════════════════════════════════════════════════════════
# TIER 2 — Gemini Market Scout
# ═══════════════════════════════════════════════════════════════════

class GeminiMarketScout:
    """Un seul appel Gemini → liste d'opportunités avec cotes."""

    def __init__(self):
        self.key = settings.gemini_api_key
        self.calls = 0

    def scout(self) -> list[dict]:
        if not self.key:
            return []

        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        text = _gemini_call_sync(_SCOUT_PROMPT.format(date=date), self.key)
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

        logger.info(f"[GeminiScout] {len(events)} opportunities (1 appel Gemini)")
        return events


# ═══════════════════════════════════════════════════════════════════
# TIER 3 — API-Sports.io (RapidAPI Hub)
# ═══════════════════════════════════════════════════════════════════

class ApiSportsClient:
    """
    Client API-Sports.io via RapidAPI.
    Un compte RapidAPI → Soccer + NBA + Tennis avec la même clé.
    Plans gratuits : 100 requêtes/jour.
    """

    def __init__(self):
        self.key  = settings.api_football_key or settings.rapidapi_key
        self.used = 0
        self._headers = {"x-apisports-key": self.key, "x-rapidapi-key": self.key} if self.key else {}

    async def _get(self, base: str, endpoint: str, params: dict, client: httpx.AsyncClient) -> dict:
        if not self.key or not self._headers:
            return {}
        try:
            r = await client.get(
                f"{base}{endpoint}",
                headers=self._headers,
                params=params,
                timeout=12.0,
            )
            self.used += 1
            if r.status_code == 200:
                return r.json()
            logger.debug(f"ApiSports {endpoint}: HTTP {r.status_code}")
        except Exception as e:
            logger.debug(f"ApiSports {endpoint}: {e}")
        return {}

    async def get_soccer_fixtures(self, client: httpx.AsyncClient, hours: int = 48) -> list[dict]:
        if not self.key:
            return []
        now    = datetime.now(timezone.utc)
        cutoff = now + timedelta(hours=hours)
        d_from = now.strftime("%Y-%m-%d")
        d_to   = cutoff.strftime("%Y-%m-%d")

        data = await self._get(AF_BASE, "/fixtures", {
            "from": d_from, "to": d_to, "timezone": "UTC", "status": "NS",
        }, client)

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
                    "id":            f"afsoccer_{f.get('id', _eid('af', home, away, ts[:10]))}",
                    "sport_key":     sport,
                    "home_team":     home,
                    "away_team":     away,
                    "commence_time": t.isoformat(),
                    "bookmakers":    [],
                    "_source":       "api_football",
                })
            except Exception:
                pass

        logger.info(f"[API-Sports] Soccer: {len(events)} fixtures")
        return events

    async def get_nba_games(self, client: httpx.AsyncClient, hours: int = 48) -> list[dict]:
        if not self.key:
            return []
        events: list[dict] = []
        now    = datetime.now(timezone.utc)
        cutoff = now + timedelta(hours=hours)

        for delta in range(2):
            d = (now + timedelta(days=delta)).strftime("%Y-%m-%d")
            data = await self._get(BBALL_BASE, "/games", {
                "date": d, "league": NBA_LEAGUE_ID, "timezone": "UTC",
            }, client)
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

        logger.info(f"[API-Sports] NBA: {len(events)} games")
        return events

    async def get_tennis_matches(self, client: httpx.AsyncClient, hours: int = 48) -> list[dict]:
        """Récupère les matchs tennis ATP/WTA à venir."""
        if not self.key:
            return []
        events: list[dict] = []
        now    = datetime.now(timezone.utc)
        cutoff = now + timedelta(hours=hours)

        for tour in (TENNIS_ATP_ID, TENNIS_WTA_ID):
            d = now.strftime("%Y-%m-%d")
            data = await self._get(TENNIS_BASE, "/games", {
                "date": d, "league": tour, "timezone": "UTC",
            }, client)
            for g in data.get("response", []):
                try:
                    status = g.get("status", {}).get("short", "")
                    if status not in ("NS", ""):
                        continue
                    ts = g.get("date", "")
                    t  = _parse_iso(ts)
                    if not t or t < now or t > cutoff:
                        continue
                    home = g.get("home", {}).get("name", "").strip()
                    away = g.get("away", {}).get("name", "").strip()
                    if not home or not away:
                        continue
                    sport_key = "tennis_atp_masters_1000" if tour == TENNIS_ATP_ID else "tennis_wta"
                    events.append({
                        "id":            f"tennis_{g.get('id', _eid('ten', home, away, d))}",
                        "sport_key":     sport_key,
                        "home_team":     home,
                        "away_team":     away,
                        "commence_time": t.isoformat(),
                        "bookmakers":    [],
                        "_source":       f"api_sports_{tour}",
                    })
                except Exception:
                    pass

        logger.info(f"[API-Sports] Tennis: {len(events)} matches")
        return events


# ═══════════════════════════════════════════════════════════════════
# TIER 4 — TheSportsDB (gratuit, aucune clé)
# ═══════════════════════════════════════════════════════════════════

class TheSportsDBClient:
    """Fixtures gratuits pour 9 grandes ligues — sans clé API."""

    async def get_upcoming_events(self, client: httpx.AsyncClient, hours: int = 48) -> list[dict]:
        events: list[dict] = []
        now    = datetime.now(timezone.utc)
        cutoff = now + timedelta(hours=hours)

        for league_id, sport_key in TSDB_LEAGUES:
            try:
                r = await client.get(
                    f"{TSDB_BASE}/eventsnextleague.php",
                    params={"id": league_id},
                    timeout=8,
                )
                if r.status_code != 200:
                    continue
                data = r.json()
                for ev in (data.get("events") or []):
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
                # Rate-limit doux entre les ligues
                await asyncio.sleep(0.2)
            except Exception as e:
                logger.debug(f"TSDB {league_id}: {e}")

        logger.info(f"[TheSportsDB] {len(events)} events")
        return events


# ═══════════════════════════════════════════════════════════════════
# TIER 5 — Gemini Odds Enricher
# ═══════════════════════════════════════════════════════════════════

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


class GeminiOddsEnricher:
    """Enrichit les fixtures sans cotes via Gemini (dernier recours)."""

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

            prompt = _ENRICH_PROMPT.format(home=home, away=away, sport=sport, date=date)
            text = _gemini_call_sync(prompt, self.key, model=GEMINI_ENRICH_MODEL, max_tokens=600)
            self.calls += 1

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
# ORCHESTRATEUR — MultiSourceFetcher (Fallback Chain)
# ═══════════════════════════════════════════════════════════════════

class MultiSourceFetcher:
    """
    Cascade de sources pour maximiser la couverture sans épuiser le quota.
    
    Utilisé par OddsFetcher quand The-Odds-API retourne 0 événements
    ou quand le quota est épuisé.
    
    Ordre:
    1. Gemini Market Scout (1 appel → top opportunités AVEC cotes)
    2. API-Sports soccer + NBA + Tennis (via RapidAPI)
    3. TheSportsDB (gratuit 0 clé) → enrichi par Gemini si budget disponible
    """

    MAX_GEMINI_ENRICH = 15  # Budget max d'enrichissement par run

    def __init__(self):
        self.scout     = GeminiMarketScout()
        self.enricher  = GeminiOddsEnricher()
        self.apisports = ApiSportsClient()
        self.tsdb      = TheSportsDBClient()
        self._stats    = {}
        self._http     = None

    async def __aenter__(self):
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(15.0),
            headers={"User-Agent": "PredatorPAIM/2.0"},
        )
        return self

    async def __aexit__(self, *_) -> None:
        if self._http:
            await self._http.aclose()

    async def fetch_all(self) -> list[dict]:
        """
        Exécute la cascade complète de sources.
        Chaque étape n'est appelée que si la précédente n'a rien donné.
        """
        all_events: list[dict] = []
        seen: set[str] = set()

        def add(evs: list[dict]):
            for e in evs:
                eid = e.get("id", "")
                if eid and eid not in seen:
                    seen.add(eid)
                    all_events.append(e)

        client = self._http
        if not client:
            async with httpx.AsyncClient() as c:
                client = c

        # ── Étape 1 : Gemini Market Scout ─────────────────────────
        logger.info("🔍 [MultiSource] Tier 2: Gemini Market Scout...")
        scout_events = self.scout.scout()
        add(scout_events)

        gemini_budget = self.MAX_GEMINI_ENRICH

        # ── Étape 2 : API-Sports (RapidAPI Hub) ───────────────────
        logger.info("🏟️ [MultiSource] Tier 3: API-Sports (RapidAPI)...")
        soccer   = await self.apisports.get_soccer_fixtures(client)
        nba       = await self.apisports.get_nba_games(client)
        tennis    = await self.apisports.get_tennis_matches(client)
        api_events = soccer + nba + tennis

        # Ne garder que ceux pas déjà vus
        new_api = [f for f in api_events if f.get("id") not in seen]
        logger.info(f"[MultiSource] {len(new_api)} nouveaux fixtures API-Sports")

        if new_api and gemini_budget > 2:
            enrich_budget = min(gemini_budget, 10)
            enriched = self.enricher.enrich(new_api, max_calls=enrich_budget)
            gemini_budget -= self.enricher.calls
            add(enriched)

        # ── Étape 3 : TheSportsDB ─────────────────────────────────
        logger.info("📊 [MultiSource] Tier 4: TheSportsDB...")
        tsdb_events = await self.tsdb.get_upcoming_events(client)
        new_tsdb = [
            e for e in tsdb_events
            if e.get("id") not in seen
        ]

        if new_tsdb and gemini_budget > 2:
            enrich_budget = min(gemini_budget, 8)
            enriched_tsdb = self.enricher.enrich(new_tsdb, max_calls=enrich_budget)
            add(enriched_tsdb)

        self._stats = {
            "total_events":      len(all_events),
            "gemini_scout_calls": self.scout.calls,
            "gemini_enrich_calls": self.enricher.calls,
            "api_sports_calls":   self.apisports.used,
            "scout_events":       len(scout_events),
            "sources":            list({e.get("_source", "?") for e in all_events}),
        }

        logger.info(
            f"✅ [MultiSource] {len(all_events)} events | "
            f"{self.scout.calls + self.enricher.calls} Gemini calls | "
            f"{self.apisports.used} API-Sports calls"
        )
        return all_events

    def get_stats(self) -> dict:
        return self._stats