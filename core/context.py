"""
core/context.py — Contexte NewsAPI pour la validation des signaux
Fournit get_market_news() (fonction simple) et ContextNewsAPI (classe async)
utilisée comme fallback dans perplexity_client.py.
"""
from __future__ import annotations

import logging
import os

import requests

logger = logging.getLogger(__name__)


# ── Fonction simple (utilisée par les anciens appels) ─────────────

def get_market_news(match_name: str, sport_title: str) -> str:
    """
    Récupère les actualités récentes pour un match via NewsAPI.
    Retourne une chaîne de titres séparés par '. '.
    """
    api_key = os.environ.get("NEWS_API_KEY")
    if not api_key:
        return "NewsAPI Key manquante."

    query = f"{match_name} {sport_title}"
    url = (
        f"https://newsapi.org/v2/everything"
        f"?q={query}&sortBy=publishedAt&apiKey={api_key}"
    )

    try:
        response = requests.get(url, timeout=10)
        data = response.json()

        if data.get("status") == "ok" and data.get("articles"):
            headlines = [a["title"] for a in data["articles"][:3]]
            return ". ".join(headlines)
        return "Aucune actualité récente trouvée."
    except Exception as e:
        return f"Erreur de récupération des news : {str(e)}"


# ── Classe async (utilisée comme fallback dans perplexity_client.py) ─

class ContextNewsAPI:
    """
    Client NewsAPI async pour la vérification contextuelle.
    Utilisé comme fallback quand Perplexity n'est pas configuré.
    """

    def __init__(self):
        self.api_key = os.environ.get("NEWS_API_KEY", "")

    async def fetch_context(
        self,
        event_name: str,
        sport_key: str,
        hours_before: int = 6,
    ) -> dict:
        """
        Récupère le contexte NewsAPI pour un événement.

        Returns:
            dict avec les clés :
              - headlines: list[str]
              - injuries: list[dict]  (articles contenant "injur" ou "blessure")
              - has_critical_injury: bool
        """
        if not self.api_key:
            logger.warning("NEWS_API_KEY manquante — contexte non disponible.")
            return {"headlines": [], "injuries": [], "has_critical_injury": False}

        query = f"{event_name} {sport_key}".strip()
        url = (
            f"https://newsapi.org/v2/everything"
            f"?q={query}&sortBy=publishedAt&pageSize=10&apiKey={self.api_key}"
        )

        try:
            # NewsAPI n'a pas de client async officiel — on utilise requests
            # dans un executor pour ne pas bloquer la boucle asyncio
            import asyncio
            import functools

            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                functools.partial(requests.get, url, timeout=10),
            )
            data = response.json()

            if data.get("status") != "ok":
                return {"headlines": [], "injuries": [], "has_critical_injury": False}

            articles = data.get("articles", [])
            headlines = [a["title"] for a in articles if a.get("title")]

            # Détecter les articles liés aux blessures
            injury_keywords = ("injur", "blessure", "blessé", "out", "ruled out", "absent")
            injuries = [
                {"title": a["title"], "url": a.get("url", "")}
                for a in articles
                if any(kw in (a.get("title") or "").lower() for kw in injury_keywords)
            ]

            return {
                "headlines": headlines[:5],
                "injuries": injuries,
                "has_critical_injury": len(injuries) > 0,
            }

        except Exception as e:
            logger.error(f"Erreur ContextNewsAPI: {e}")
            return {"headlines": [], "injuries": [], "has_critical_injury": False}
