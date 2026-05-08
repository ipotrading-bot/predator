"""
core/news_engine.py — NewsAPI Market-Moving Filter
Scanne NewsAPI pour les mots-clés "Market Moving" et construit
un score d'impact pour chaque match détecté.

Doctrine PhD MIT :
  - Seules les news < 6h sont pertinentes (Alpha Decay)
  - Mots-clés pondérés : blessure > suspension > météo > rumeur
  - Score 0.0–1.0 : 0 = aucun impact, 1.0 = impact maximal confirmé
"""
from __future__ import annotations

import asyncio
import functools
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# ── Mots-clés pondérés par impact ────────────────────────────────
IMPACT_KEYWORDS: dict[str, float] = {
    # Blessures (impact maximal)
    "injury": 1.0,
    "injured": 1.0,
    "out": 0.9,
    "ruled out": 1.0,
    "doubtful": 0.7,
    "questionable": 0.6,
    "blessure": 1.0,
    "blessé": 1.0,
    "forfait": 1.0,
    "absent": 0.8,
    # Suspensions
    "suspended": 0.9,
    "suspension": 0.9,
    "ban": 0.8,
    "red card": 0.7,
    # Changements tactiques
    "sacked": 0.8,
    "fired": 0.8,
    "coach change": 0.7,
    "lineup change": 0.6,
    "rotation": 0.4,
    # Météo (outdoor uniquement)
    "postponed": 1.0,
    "cancelled": 1.0,
    "heavy rain": 0.5,
    "snow": 0.5,
    "wind": 0.3,
    # Rumeurs (faible poids)
    "rumor": 0.2,
    "rumour": 0.2,
    "report": 0.15,
}

# Sports outdoor (météo pertinente)
OUTDOOR_SPORTS = {
    "soccer_epl", "soccer_spain_la_liga", "soccer_uefa_champs_league",
    "tennis_atp", "tennis_wta",
}


@dataclass
class NewsImpact:
    """Résultat de l'analyse NewsAPI pour un match."""
    match_name: str
    sport: str
    impact_score: float          # 0.0 – 1.0
    market_moving: bool          # True si impact_score > 0.5
    top_headlines: list[str] = field(default_factory=list)
    injury_alerts: list[str] = field(default_factory=list)
    sources_checked: int = 0
    latency_ms: int = 0

    @property
    def summary(self) -> str:
        if not self.market_moving:
            return "✅ Aucune news market-moving détectée."
        top = self.injury_alerts[0] if self.injury_alerts else (
            self.top_headlines[0] if self.top_headlines else "Impact détecté"
        )
        return f"⚠️ NEWS IMPACT ({self.impact_score:.0%}) : {top[:120]}"


class NewsEngine:
    """
    Moteur NewsAPI pour la détection des événements market-moving.

    Usage :
        engine = NewsEngine()
        impact = await engine.analyze_match("Lakers vs Celtics", "basketball_nba")
        if impact.market_moving:
            # rejeter ou ajuster le signal
    """

    # Cache TTL : 10 minutes (les news bougent vite)
    _CACHE_TTL = 600

    def __init__(self):
        self.api_key = os.environ.get("NEWS_API_KEY", "")
        self._cache: dict[str, tuple[float, NewsImpact]] = {}
        self._request_count = 0

    def is_available(self) -> bool:
        return bool(self.api_key)

    # ─────────────────────────────────────────────────────────────
    # Interface principale
    # ─────────────────────────────────────────────────────────────

    async def analyze_match(
        self,
        match_name: str,
        sport: str,
        hours_back: int = 6,
    ) -> NewsImpact:
        """
        Analyse les news pour un match donné.
        Retourne un NewsImpact avec score et alertes.
        """
        cache_key = f"{match_name}_{sport}_{hours_back}"
        cached = self._get_cached(cache_key)
        if cached:
            return cached

        if not self.is_available():
            return NewsImpact(
                match_name=match_name,
                sport=sport,
                impact_score=0.0,
                market_moving=False,
                top_headlines=[],
                injury_alerts=[],
                sources_checked=0,
            )

        start = time.monotonic()

        # Construire la requête : noms d'équipes + mots-clés sport
        teams = match_name.replace(" vs ", " OR ")
        query = f'({teams}) AND (injury OR suspended OR out OR blessure)'

        articles = await self._fetch_articles(query, hours_back)
        impact = self._compute_impact(match_name, sport, articles)
        impact.latency_ms = int((time.monotonic() - start) * 1000)

        self._set_cache(cache_key, impact)
        return impact

    async def batch_analyze(
        self,
        matches: list[tuple[str, str]],  # [(match_name, sport), ...]
        hours_back: int = 6,
    ) -> dict[str, NewsImpact]:
        """
        Analyse plusieurs matchs en parallèle (max 5 concurrents pour respecter le rate limit).
        Retourne un dict {match_name: NewsImpact}.
        """
        semaphore = asyncio.Semaphore(5)

        async def _analyze_one(match_name: str, sport: str) -> tuple[str, NewsImpact]:
            async with semaphore:
                impact = await self.analyze_match(match_name, sport, hours_back)
                return match_name, impact

        tasks = [_analyze_one(name, sport) for name, sport in matches]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        output = {}
        for res in results:
            if isinstance(res, Exception):
                logger.warning(f"NewsEngine batch error: {res}")
            else:
                name, impact = res
                output[name] = impact

        return output

    # ─────────────────────────────────────────────────────────────
    # Fetch NewsAPI
    # ─────────────────────────────────────────────────────────────

    async def _fetch_articles(
        self, query: str, hours_back: int
    ) -> list[dict]:
        """Appel NewsAPI /everything en async via executor."""
        from datetime import datetime, timedelta, timezone

        since = (datetime.now(timezone.utc) - timedelta(hours=hours_back)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

        params = {
            "q": query,
            "from": since,
            "sortBy": "publishedAt",
            "language": "en",
            "pageSize": 20,
            "apiKey": self.api_key,
        }

        try:
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                functools.partial(
                    requests.get,
                    "https://newsapi.org/v2/everything",
                    params=params,
                    timeout=10,
                ),
            )
            self._request_count += 1
            data = response.json()

            if data.get("status") != "ok":
                logger.warning(f"NewsAPI status: {data.get('status')} | {data.get('message', '')}")
                return []

            return data.get("articles", [])

        except Exception as e:
            logger.error(f"NewsAPI fetch error: {e}")
            return []

    # ─────────────────────────────────────────────────────────────
    # Calcul du score d'impact
    # ─────────────────────────────────────────────────────────────

    def _compute_impact(
        self, match_name: str, sport: str, articles: list[dict]
    ) -> NewsImpact:
        """
        Calcule le score d'impact à partir des articles.
        Score = max des poids des mots-clés trouvés (pas une somme — évite l'inflation).
        """
        if not articles:
            return NewsImpact(
                match_name=match_name,
                sport=sport,
                impact_score=0.0,
                market_moving=False,
                sources_checked=0,
            )

        max_score = 0.0
        headlines = []
        injury_alerts = []

        for article in articles:
            title = (article.get("title") or "").lower()
            description = (article.get("description") or "").lower()
            text = f"{title} {description}"

            # Ignorer météo pour sports indoor
            if sport not in OUTDOOR_SPORTS:
                weather_keys = {"heavy rain", "snow", "wind", "postponed"}
                # Ne pas scorer les mots météo pour sports indoor
                relevant_keywords = {
                    k: v for k, v in IMPACT_KEYWORDS.items()
                    if k not in weather_keys
                }
            else:
                relevant_keywords = IMPACT_KEYWORDS

            article_score = 0.0
            for keyword, weight in relevant_keywords.items():
                if keyword in text:
                    article_score = max(article_score, weight)

            if article_score > 0:
                headline = article.get("title", "")[:150]
                headlines.append(headline)
                if article_score >= 0.7:
                    injury_alerts.append(headline)
                max_score = max(max_score, article_score)

        return NewsImpact(
            match_name=match_name,
            sport=sport,
            impact_score=round(max_score, 3),
            market_moving=max_score >= 0.5,
            top_headlines=headlines[:5],
            injury_alerts=injury_alerts[:3],
            sources_checked=len(articles),
        )

    # ─────────────────────────────────────────────────────────────
    # Cache
    # ─────────────────────────────────────────────────────────────

    def _get_cached(self, key: str) -> Optional[NewsImpact]:
        if key in self._cache:
            ts, impact = self._cache[key]
            if time.time() - ts < self._CACHE_TTL:
                return impact
        return None

    def _set_cache(self, key: str, impact: NewsImpact) -> None:
        self._cache[key] = (time.time(), impact)
        # Nettoyage si cache trop grand
        if len(self._cache) > 200:
            oldest = sorted(self._cache, key=lambda k: self._cache[k][0])[:100]
            for k in oldest:
                del self._cache[k]

    def get_stats(self) -> dict:
        return {
            "available": self.is_available(),
            "total_requests": self._request_count,
            "cache_size": len(self._cache),
        }


# Singleton
news_engine = NewsEngine()
