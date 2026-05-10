"""
data/odds_fetcher.py — v5.5 Global Upcoming (sync, requests)
Endpoint: /sports/upcoming/odds/ — tous sports en 1 appel, filtre 48h côté scanner.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone, timedelta
import requests as req

from config import settings

logger = logging.getLogger(__name__)

BASE_URL = "https://api.the-odds-api.com/v4"


class OddsFetcher:
    """
    Client synchrone pour The-Odds-API v5.5.
    Un seul appel à /sports/upcoming/odds/ pour TOUS les sports.
    """

    def __init__(self):
        self.api_key = settings.odds_api_key
        self._remaining_requests: int = -1
        self._requests_used: int = -1

    def _get(self, endpoint: str, params: dict) -> list:
        """Requête GET avec retry 3x et gestion des erreurs HTTP."""
        params = dict(params)
        params["apiKey"] = self.api_key
        url = f"{BASE_URL}{endpoint}"

        for attempt in range(3):
            try:
                resp = req.get(url, params=params, timeout=15)
                self._remaining_requests = int(resp.headers.get("x-requests-remaining", -1))
                self._requests_used = int(resp.headers.get("x-requests-used", -1))

                if resp.status_code == 401:
                    raise ValueError("Clé API invalide ou expirée (401)")
                if resp.status_code == 422:
                    raise ValueError(f"Paramètres invalides (422): {resp.text[:100]}")
                if resp.status_code == 429:
                    logger.warning("Rate limit (429) — pause 5s")
                    time.sleep(5)
                    continue
                if resp.status_code != 200:
                    logger.error(f"API erreur {resp.status_code}: {resp.text[:200]}")
                    return []

                data = resp.json()
                return data if isinstance(data, list) else []

            except req.RequestException as e:
                logger.error(f"Requête échouée (attempt {attempt + 1}/3): {e}")
                if attempt < 2:
                    time.sleep(2 ** attempt)

        return []

    def fetch_upcoming_odds(self) -> list[dict]:
        """
        Endpoint Global: /sports/upcoming/odds/
        IMPORTANT: Cet endpoint n'accepte PAS le paramètre 'bookmakers' en même temps
        que 'regions'. On utilise uniquement regions=eu,us et on filtre en Python.
        On ajoute commenceTimeTo pour ne récupérer que les 48h à venir.
        """
        now = datetime.now(timezone.utc)
        cutoff_48h = (now + timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%SZ")

        params = {
            "regions": "eu,us",
            "markets": "h2h,spreads,totals",
            "oddsFormat": "decimal",
            "commenceTimeTo": cutoff_48h,
        }

        logger.info(f"🚀 UPCOMING GLOBAL: 1 appel API | fenêtre jusqu'à {cutoff_48h}")
        events = self._get("/sports/upcoming/odds/", params)

        if events:
            sports_found = {e.get("sport_key", "?") for e in events}
            with_bm = sum(1 for e in events if e.get("bookmakers"))
            logger.info(
                f"[DEBUG] {len(events)} events | {len(sports_found)} sports | "
                f"{with_bm} avec bookmakers | quota restant: {self._remaining_requests}"
            )
        else:
            logger.warning("[DEBUG] 0 events — quota épuisé ou aucun match dans les 48h")

        return events

    def fetch_sport_odds(self, sport: str) -> list[dict]:
        """
        Endpoint par sport pour la rotation (protocole rate-limit).
        Utilise bookmakers= sur l'endpoint /sports/{sport}/odds/ (supporté ici).
        """
        all_books = settings.sharp_books + settings.soft_books
        params = {
            "bookmakers": ",".join(all_books),
            "markets": "h2h,spreads,totals",
            "oddsFormat": "decimal",
        }
        return self._get(f"/sports/{sport}/odds/", params)

    @staticmethod
    def cleanup_expired_signals(supabase_client) -> int:
        """Supprime les signaux dont le match est passé ou à >48h."""
        try:
            now = datetime.now(timezone.utc)
            cutoff_past = (now - timedelta(hours=1)).isoformat()
            cutoff_future = (now + timedelta(hours=48)).isoformat()

            res_past = (
                supabase_client._client.table("signals")
                .delete()
                .lt("match_time", cutoff_past)
                .execute()
            )
            res_future = (
                supabase_client._client.table("signals")
                .delete()
                .gt("match_time", cutoff_future)
                .execute()
            )

            total = len(res_past.data or []) + len(res_future.data or [])
            if total:
                logger.info(f"🧹 Nettoyage: {total} signaux expirés supprimés")
            return total
        except Exception as e:
            logger.error(f"Erreur nettoyage signaux: {e}")
            return 0

    def fetch_all_upcoming(self) -> list[dict]:
        """
        Stratégie double:
        1. Essaie l'endpoint global /sports/upcoming/odds/
        2. Si 0 résultats → fallback parallèle par sport (ThreadPoolExecutor)
        """
        events = self.fetch_upcoming_odds()
        if events:
            return events

        logger.warning("⚠️ Global endpoint vide → fallback scan parallèle par sport")
        return self._parallel_sport_scan(settings.target_sports[:10])

    def _parallel_sport_scan(self, sports: list[str]) -> list[dict]:
        """Scan N sports en parallèle (ThreadPoolExecutor, max 3 workers)."""
        from concurrent.futures import ThreadPoolExecutor, as_completed
        all_events: list[dict] = []

        with ThreadPoolExecutor(max_workers=3) as ex:
            futures = {ex.submit(self.fetch_sport_odds, s): s for s in sports}
            for fut in as_completed(futures, timeout=30):
                try:
                    all_events.extend(fut.result() or [])
                except Exception as e:
                    logger.warning(f"Scan sport échoué: {futures[fut]} — {e}")

        seen: set[str] = set()
        unique: list[dict] = []
        for e in all_events:
            eid = e.get("id", "")
            if eid and eid not in seen:
                seen.add(eid)
                unique.append(e)

        logger.info(f"🔄 Parallel scan: {len(unique)} événements uniques ({len(sports)} sports)")
        return unique

    def get_quota_status(self) -> dict:
        return {
            "remaining_requests": self._remaining_requests,
            "requests_used": self._requests_used,
        }
