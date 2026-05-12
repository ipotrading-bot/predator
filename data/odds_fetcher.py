"""
data/odds_fetcher.py — Récupération des cotes via The-Odds-API
Rate limiter intégré : 15 RPM max (1 req / 4s)
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from config import settings
from core.paim_engine import MarketOdds

logger = logging.getLogger(__name__)

BASE_URL = "https://api.the-odds-api.com/v4"


class RateLimiter:
    """Token bucket limité à N requêtes par minute."""

    def __init__(self, requests_per_minute: int):
        self.rpm = requests_per_minute
        self.min_interval = 60.0 / requests_per_minute
        self._last_call: float = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_call
            wait = self.min_interval - elapsed
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call = time.monotonic()


class OddsFetcher:
    """
    Client async pour The-Odds-API avec:
    - Rate limiting (15 RPM)
    - Retry exponentiel
    - Parsing vers MarketOdds
    """

    def __init__(self):
        self.api_key = settings.odds_api_key
        self.rate_limiter = RateLimiter(settings.api_requests_per_minute)
        self._client: Optional[httpx.AsyncClient] = None
        self._remaining_requests: int = -1
        self._remaining_month: int = -1

    async def __aenter__(self) -> "OddsFetcher":
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(10.0),
            headers={"User-Agent": "PredatorPAIM/1.0"},
        )
        return self

    async def __aexit__(self, *_) -> None:
        if self._client:
            await self._client.aclose()

    # ── Requêtes brutes ───────────────────────────────────────

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
    async def _get(self, endpoint: str, params: dict) -> dict:
        await self.rate_limiter.acquire()
        params["apiKey"] = self.api_key
        response = await self._client.get(f"{BASE_URL}{endpoint}", params=params)

        # ── CORRECTIF #1 : Vérification stricte du statut HTTP ────────────
        if response.status_code != 200:
            logger.error(
                f"❌ Erreur API {response.status_code} sur {endpoint}: "
                f"{response.text[:200]}"
            )
            response.raise_for_status()

        # ── CORRECTIF #2 : Détection de réponse HTML (Unexpected token <) ─
        content_type = response.headers.get("content-type", "")
        if "text/html" in content_type or response.text.strip().startswith("<"):
            logger.error(
                f"❌ Réponse HTML reçue au lieu de JSON sur {endpoint}: "
                f"{response.text[:200]}"
            )
            raise ValueError(
                f"Réponse HTML reçue (status={response.status_code}). "
                f"Attendu JSON. Vérifiez l'URL et la clé API."
            )

        self._remaining_requests = int(
            response.headers.get("x-requests-remaining", -1)
        )
        self._remaining_month = int(
            response.headers.get("x-requests-used", -1)
        )
        logger.debug(f"API quota restant: {self._remaining_requests} requêtes")
        return response.json()

    # ── Endpoints ─────────────────────────────────────────────

    async def fetch_odds(
        self,
        sport: str,
        markets: str = "h2h,spreads",
        bookmakers: Optional[list[str]] = None,
        regions: str = "eu",
    ) -> list[dict]:
        """Récupère toutes les cotes pour un sport donné."""
        params: dict = {
            "sport": sport,
            "regions": regions,
            "markets": markets,
            "oddsFormat": "decimal",
        }
        if bookmakers:
            params["bookmakers"] = ",".join(bookmakers)
        return await self._get(f"/sports/{sport}/odds/", params)

    async def fetch_odds_for_sport(self, sport: str) -> list[dict]:
        """
        Récupère les cotes pour UN sport spécifique (protocole rotation).
        
        Pulse Hunter v6.0: Inclut les marchés Props (Player Points, Rebounds, Corners)
        + Binary Markets (BTTS, h2h_lay, totals)
        
        Args:
            sport: Clé The-Odds-API (ex: 'basketball_nba')
            
        Returns:
            list[dict]: Liste des événements avec cotes
        """
        all_books = settings.sharp_books + settings.soft_books
        
        # Pulse Hunter: Expanded markets for high-frequency props and binary markets
        markets = "h2h,spreads,totals"
        
        # Add player props for NBA
        if "basketball" in sport:
            markets += ",player_points,player_rebounds"
        # Add corner props and BTTS for soccer
        elif "soccer" in sport:
            markets += ",total_corners,btts"
        
        # Add h2h_lay (Double Chance) for all sports
        markets += ",h2h_lay"
        
        return await self.fetch_odds(
            sport=sport,
            markets=markets,
            bookmakers=all_books,
        )

    async def fetch_all_sports_odds(self) -> list[dict]:
        """Scan complet de tous les sports cibles en parallèle."""
        all_books = settings.sharp_books + settings.soft_books

        tasks = [
            self.fetch_odds(
                sport=sport,
                markets="h2h,spreads,totals",
                bookmakers=all_books,
            )
            for sport in settings.target_sports
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        events = []
        for sport, result in zip(settings.target_sports, results):
            if isinstance(result, Exception):
                logger.error(f"Erreur fetch {sport}: {result}")
            else:
                events.extend(result)
        return events

    # ── Parsing ───────────────────────────────────────────────

    @staticmethod
    def parse_to_market_odds(
        event: dict,
        bookmaker_key: str,
        market_key: str = "h2h",
    ) -> Optional[MarketOdds]:
        """
        Parse un événement API vers MarketOdds.
        Retourne None si le bookmaker ou marché est absent.
        """
        for bm in event.get("bookmakers", []):
            if bm["key"] != bookmaker_key:
                continue
            for market in bm.get("markets", []):
                if market["key"] != market_key:
                    continue
                outcomes = market.get("outcomes", [])
                if len(outcomes) < 2:
                    continue
                home_odds = next(
                    (o["price"] for o in outcomes if o["name"] == event.get("home_team")),
                    outcomes[0]["price"],
                )
                away_odds = next(
                    (o["price"] for o in outcomes if o["name"] == event.get("away_team")),
                    outcomes[1]["price"],
                )
                return MarketOdds(
                    bookmaker=bookmaker_key,
                    event_id=event["id"],
                    market_key=market_key,
                    outcome_home=home_odds,
                    outcome_away=away_odds,
                    timestamp=time.time(),
                )
        return None

    def get_quota_status(self) -> dict:
        return {
            "remaining_requests": self._remaining_requests,
            "requests_used_month": self._remaining_month,
        }
