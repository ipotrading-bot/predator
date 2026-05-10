"""
data/odds_fetcher.py — Récupération des cotes via The-Odds-API v5.5
Rate limiter intégré : 15 RPM max (1 req / 4s)

Mode "Upcoming Global" — Un seul appel API pour TOUS les sports.
Réécriture totale pour débloquer le moteur de scan.
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
    Client async pour The-Odds-API v5.5 — Mode Upcoming Global.
    
    Changements v5.5:
    - Un seul appel à l'endpoint `upcoming` au lieu de 30+ appels par sport
    - Retourne TOUS les sports, TOUS les marchés (h2h, spreads, totals)
    - Filtrage temporel délégué au scanner
    - Log de diagnostic X/Y/Z pour le dashboard
    - Nettoyage des signaux expirés
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

        if response.status_code != 200:
            logger.error(
                f"❌ Erreur API {response.status_code} sur {endpoint}: "
                f"{response.text[:200]}"
            )
            response.raise_for_status()

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

    # ── Endpoint UPCOMING (unique, tous sports) ────────────────

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
        """
        all_books = settings.sharp_books + settings.soft_books
        return await self.fetch_odds(
            sport=sport,
            markets="h2h,spreads,totals",
            bookmakers=all_books,
        )

    async def fetch_all_sports_odds(self) -> list[dict]:
        """
        🚀 MODE UPCOMING GLOBAL v5.5 — Un seul appel pour TOUS les sports.
        
        Utilise l'endpoint /sports/upcoming/odds/ qui retourne tous les matchs
        à venir dans toutes les disciplines, dans une seule réponse JSON.
        
        Avantages:
        - 1 requête API au lieu de 30+
        - Pas de trous entre les sports
        - Consomme 1/30ème du quota
        - Détecte automatiquement les nouveaux sports
        
        Returns:
            list[dict]: Tous les événements upcoming avec cotes
        """
        all_books = settings.sharp_books + settings.soft_books
        
        logger.info("🚀 UPCOMING GLOBAL: 1 appel API pour tous les sports")
        
        events = await self.fetch_odds(
            sport="upcoming",
            markets="h2h,spreads,totals",
            bookmakers=all_books,
            regions="eu,us",  # Les deux régions pour max couverture
        )
        
        # Log de diagnostic
        if events:
            sports_found = set(e.get("sport_key", "unknown") for e in events)
            # Compter les événements binaires potentiels (ceux avec bookmakers)
            with_bookmakers = sum(1 for e in events if e.get("bookmakers"))
            logger.info(
                f"[DEBUG] {len(events)} matchs reçus de l'API | "
                f"{len(sports_found)} sports détectés | "
                f"{with_bookmakers} avec bookmakers"
            )
            logger.info(f"[DEBUG] Sports: {', '.join(sorted(sports_found)[:10])}{'...' if len(sports_found) > 10 else ''}")
        else:
            logger.warning("[DEBUG] 0 matchs reçus de l'API — clé API ou quota invalide")
        
        return events

    # ── Nettoyage Supabase ─────────────────────────────────────

    @staticmethod
    async def cleanup_expired_signals(supabase_client) -> int:
        """
        🔧 NETTOYAGE v5.5
        Supprime les signaux expirés (plus de 48h dans le futur ou date passée).
        
        Returns:
            int: Nombre de signaux supprimés
        """
        try:
            from datetime import datetime, timezone, timedelta
            now = datetime.now(timezone.utc)
            cutoff = (now - timedelta(hours=1)).isoformat()  # Matchs commencés il y a >1h
            future = (now + timedelta(hours=48)).isoformat()  # Matchs à plus de 48h
            
            # Supprimer les matchs passés
            res_past = supabase_client.table('signals')\
                .delete()\
                .lt('match_time', cutoff)\
                .execute()
            
            # Supprimer les matchs trop lointains
            res_future = supabase_client.table('signals')\
                .delete()\
                .gt('match_time', future)\
                .execute()
            
            total = len(res_past.data or []) + len(res_future.data or [])
            if total > 0:
                logger.info(f"🧹 Nettoyage: {total} signaux expirés supprimés")
            return total
        except Exception as e:
            logger.error(f"Erreur nettoyage signaux: {e}")
            return 0

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