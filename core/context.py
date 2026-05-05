"""
core/context.py v2.2 — Multi-LLM Context Pipeline (Architecture PhD MIT)
Couches :
  1. Groq (Llama 3 70B) — Scan de vélocité VPIN (200ms)
  2. NewsAPI — Détection blessures/lineups temps réel
  3. Reddit API — Sentiment irrationnel (paris contre la foule)
  4. Gemini Flash — Synthèse finale Ticket 7/9
  5. Queue Manager — Rate limiting global 15 RPM

Doctrine : Si Pinnacle contredit les news → Pinnacle gagne. Toujours.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from config import settings

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════
# STRUCTURES DE DONNÉES
# ═══════════════════════════════════════════════════════════════════

@dataclass
class ContextSignal:
    """Signal contextuel enrichi par les sources externes."""
    event_name: str
    sport_key: str
    selection: str
    sharp_prob: float
    soft_prob: float
    ev_plus: float
    snr_ratio: float

    # Enrichissements contextuels
    injuries: list[dict] = field(default_factory=list)
    lineups: dict = field(default_factory=dict)
    sentiment_public: str = "neutral"  # bullish | bearish | neutral
    sentiment_score: float = 0.0       # -1.0 (contre) à +1.0 (pour)
    news_headlines: list[str] = field(default_factory=list)
    vpin_toxicity: str = "unknown"     # informed | noise | unknown
    market_consensus: str = "neutral"

    # Flags de rejet
    has_critical_injury: bool = False
    public_against: bool = False       # True = opportunité (paris contre la foule)
    pinnacle_contradicts_news: bool = False
    recommended_action: str = "hold"   # hold | bet | reject


@dataclass
class VPINResult:
    """Volume-Synchronized Probability of Informed Trading."""
    toxicity: str          # "informed" | "noise" | "unknown"
    confidence: float      # 0.0 - 1.0
    analysis: str          # Résumé 1 phrase


# ═══════════════════════════════════════════════════════════════════
# QUEUE MANAGER — Rate Limiting Global (15 RPM)
# ═══════════════════════════════════════════════════════════════════

class QueueManager:
    """
    Gestionnaire de file d'attente pour les appels API externes.
    Garantit max 15 requêtes/minute toutes sources confondues.
    """

    def __init__(self, rpm: int = 15):
        self.rpm = rpm
        self.min_interval = 60.0 / rpm
        self._last_call: float = 0.0
        self._lock = asyncio.Lock()
        self._queue: list = []
        self._stats: dict = {"groq": 0, "newsapi": 0, "gemini": 0, "reddit": 0}

    async def acquire(self, source: str = "unknown") -> None:
        """Attend son tour avant d'exécuter une requête."""
        async with self._lock:
            self._stats[source] = self._stats.get(source, 0) + 1
            now = time.monotonic()
            elapsed = now - self._last_call
            wait = self.min_interval - elapsed
            if wait > 0:
                logger.debug(f"⏳ QueueManager: attente {wait:.1f}s ({source})")
                await asyncio.sleep(wait)
            self._last_call = time.monotonic()

    def get_stats(self) -> dict:
        return dict(self._stats)


# ═══════════════════════════════════════════════════════════════════
# COUCHE 1 — GROQ : VPIN (Toxicité du Flux)
# ═══════════════════════════════════════════════════════════════════

class GroqVelocityScanner:
    """
    Groq Llama 3 70B — Analyse sub-seconde du mouvement de cote.
    Détecte si un mouvement est "Informed" (Toxique) ou "Public" (Bruit).
    """

    VPIN_PROMPT = """Tu es un analyste quantitatif spécialisé en microstructure de marché.
    
Analyse ce mouvement de cote et détermine s'il est "Informed" (initié par de l'argent intelligent) ou "Noise" (bruit public).

Réponds UNIQUEMENT en JSON strict :
{
  "toxicity": "informed" | "noise" | "unknown",
  "confidence": 0.0-1.0,
  "analysis": "explication 1 phrase"
}

Règle cardinale : Si Pinnacle bouge avant 1XBet → informed. Si 1XBet rattrape Pinnacle → noise.
"""

    def __init__(self, queue: Optional[QueueManager] = None):
        self._client = None
        self.queue = queue or QueueManager()

    def _get_client(self):
        if self._client is None:
            from groq import Groq
            self._client = Groq(api_key=settings.groq_api_key)
        return self._client

    async def scan_velocity(
        self,
        sharp_odds: list[float],
        soft_odds: list[float],
        sharp_prob: float,
        soft_prob: float,
    ) -> VPINResult:
        """
        Analyse la vélocité du spread Sharp → Soft pour détecter la toxicité.
        """
        gap = abs(sharp_prob - soft_prob)
        if gap < 0.02:
            return VPINResult("noise", 0.1, "Écart trop faible — bruit de marché.")

        if not settings.groq_api_key:
            # Fallback heuristique : si gap > 5%, probablement informed
            if gap > 0.05:
                return VPINResult("informed", 0.6, "Heuristique: gap > 5% — mouvement significatif.")
            return VPINResult("noise", 0.3, "Heuristique: gap < 5% — bruit probable.")

        prompt = f"""
Sharp Odds: {sharp_odds}
Soft Odds: {soft_odds}
Sharp Prob: {sharp_prob:.3f}
Soft Prob: {soft_prob:.3f}
Gap: {gap:.2%}

Ce mouvement est-il Informed ou Noise ?
"""
        try:
            await self.queue.acquire("groq")
            completion = self._get_client().chat.completions.create(
                model=settings.groq_model,  # llama-3.1-70b-versatile
                messages=[
                    {"role": "system", "content": self.VPIN_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.1,
                max_tokens=200,
            )
            data = json.loads(completion.choices[0].message.content.strip())
            return VPINResult(
                toxicity=data.get("toxicity", "unknown"),
                confidence=float(data.get("confidence", 0.0)),
                analysis=data.get("analysis", ""),
            )
        except Exception as e:
            logger.warning(f"Groq VPIN error: {e}")
            return VPINResult("unknown", 0.0, "Erreur analyse vélocité.")


# ═══════════════════════════════════════════════════════════════════
# COUCHE 2 — NEWSAPI : Blessures / Lineups
# ═══════════════════════════════════════════════════════════════════

class ContextNewsAPI:
    """
    NewsAPI / RSS — Détection d'événements critiques :
    - Blessures de dernière minute (< 6h avant match)
    - Changements de composition (lineup)
    - Conditions météo extrêmes
    """

    INJURY_KEYWORDS = [
        "injury", "injured", "out", "questionable", "doubtful",
        "probable", "day-to-day", "miss", "absent", "ruled out",
        "hamstring", "ankle", "knee", "concussion", "illness",
    ]
    LINEUP_KEYWORDS = [
        "lineup", "starting", "confirmed", "xi", "roster",
        "captain", "benched", "substitute",
    ]

    def __init__(self, queue: Optional[QueueManager] = None):
        self.queue = queue or QueueManager()

    async def fetch_context(
        self,
        event_name: str,
        sport_key: str,
        hours_before: int = 6,
    ) -> dict:
        """
        Récupère le contexte news pour un événement donné.
        Retourne un dict avec injuries, lineups, headlines.
        """
        result = {
            "injuries": [],
            "lineups": {},
            "headlines": [],
            "has_critical_injury": False,
            "weather_alerts": [],
        }

        # Extraction des noms d'équipes
        teams = []
        if " vs " in event_name:
            teams = [t.strip() for t in event_name.split(" vs ")]
        elif " - " in event_name:
            teams = [t.strip() for t in event_name.split(" - ")]
        else:
            teams = [event_name]

        # Tentative NewsAPI
        if settings.news_api_key:
            try:
                await self.queue.acquire("newsapi")
                import httpx

                query_parts = teams + [sport_key.replace("_", " ")]
                query = " OR ".join(query_parts)

                async with httpx.AsyncClient() as client:
                    resp = await client.get(
                        "https://newsapi.org/v2/everything",
                        params={
                            "q": query,
                            "from": (datetime.now() - timedelta(hours=hours_before)).isoformat(),
                            "sortBy": "relevancy",
                            "language": "en",
                            "apiKey": settings.news_api_key,
                            "pageSize": 10,
                        },
                        timeout=10,
                    )
                    data = resp.json()
                    if data.get("status") == "ok":
                        for article in data.get("articles", []):
                            title = article.get("title", "")
                            desc = article.get("description", "") or ""
                            text = f"{title} {desc}".lower()

                            # Détection blessures
                            for kw in self.INJURY_KEYWORDS:
                                if kw in text:
                                    result["injuries"].append({
                                        "title": title,
                                        "source": article["source"]["name"],
                                        "published": article["publishedAt"],
                                    })
                                    if any(k in text for k in ["ruled out", "out", "miss"]):
                                        result["has_critical_injury"] = True
                                    break

                            # Détection lineups
                            for kw in self.LINEUP_KEYWORDS:
                                if kw in text:
                                    result["lineups"][article["source"]["name"]] = title
                                    break

                            result["headlines"].append(title)

            except Exception as e:
                logger.debug(f"NewsAPI fetch error: {e}")

        return result


# ═══════════════════════════════════════════════════════════════════
# COUCHE 3 — SENTIMENT : Détection de la Foule Irrationnelle
# ═══════════════════════════════════════════════════════════════════

class SentimentAnalyzer:
    """
    Analyse le sentiment ambiant sur le marché.
    Si la foule parie massivement sur une équipe (sentiment irrationnel),
    le modèle PAIM doit parier contre la foule sur la ligne gonflée.
    
    Sources :
    - Mouvement des cotes Soft vs Sharp (delta)
    - Ratio de volume public
    """

    def __init__(self):
        pass

    async def analyze(
        self,
        sharp_prob: float,
        soft_prob: float,
        soft_odds_home: float,
        soft_odds_away: float,
    ) -> dict:
        """
        Calcule le sentiment de marché basé sur les écarts de cotes.
        
        Règle : 
        - Si Soft offre une cote significativement plus haute que Sharp
          sur le home → le public parie away (et vice versa)
        - Le sentiment est "irrationnel" quand l'écart > 5%
        """
        implied_sharp_home = sharp_prob
        implied_sharp_away = 1.0 - sharp_prob
        implied_soft_home = 1.0 / soft_odds_home if soft_odds_home > 0 else 0
        implied_soft_away = 1.0 / soft_odds_away if soft_odds_away > 0 else 0

        # Calcul de l'asymétrie de sentiment
        home_bias = implied_soft_home - implied_sharp_home
        away_bias = implied_soft_away - implied_sharp_away

        # La foule gonfle artificiellement le côté où elle parie
        # Si home_bias > 0, le public surévalue le home (signe de sentiment irrationnel)
        public_favor = "home" if home_bias > away_bias else "away"
        irrationality = max(abs(home_bias), abs(away_bias))

        sentiment = "neutral"
        if irrationality > 0.05:
            sentiment = "irrational"

        # Opportunité : parier contre la foule si sentiment irrationnel
        opportunity = "none"
        if sentiment == "irrational":
            opportunity = f"bet_against_{public_favor}"

        return {
            "public_favor": public_favor,
            "irrationality_score": round(irrationality, 4),
            "sentiment": sentiment,
            "opportunity": opportunity,
            "home_bias": round(home_bias, 4),
            "away_bias": round(away_bias, 4),
        }


# ═══════════════════════════════════════════════════════════════════
# ORCHESTREUR — Multi-LLM Context Pipeline
# ═══════════════════════════════════════════════════════════════════

class ContextAnalyzer:
    """
    Orchestre les 3 couches contextuelles :
    1. Groq (vélocité VPIN)
    2. NewsAPI (blessures/lineups)
    3. Sentiment (foule irrationnelle)
    
    Puis transmet le tout à Gemini Flash pour la synthèse finale.
    
    Doctrine PhD MIT :
    - Si Pinnacle contredit les news → Pinnacle gagne.
    - Si la foule est irrationnelle → parier contre.
    - Si blessure critique confirmée → rejet immédiat.
    """

    def __init__(self):
        self.queue = QueueManager(rpm=15)
        self.groq_scanner = GroqVelocityScanner(queue=self.queue)
        self.news_api = ContextNewsAPI(queue=self.queue)
        self.sentiment = SentimentAnalyzer()

    async def analyze(
        self,
        signal_data: ContextSignal,
        sharp_odds: list[float],
        soft_odds: list[float],
    ) -> ContextSignal:
        """
        Pipeline complet d'analyse contextuelle.
        Exécute les 3 couches en parallèle puis synthétise.
        """
        logger.info(f"🔍 Analyse contextuelle: {signal_data.event_name}")

        # ── 1. VPIN Scan (Groq) en parallèle ─────────────────────
        vpin_task = self.groq_scanner.scan_velocity(
            sharp_odds=sharp_odds,
            soft_odds=soft_odds,
            sharp_prob=signal_data.sharp_prob,
            soft_prob=signal_data.soft_prob,
        )

        # ── 2. News context en parallèle ─────────────────────────
        news_task = self.news_api.fetch_context(
            event_name=signal_data.event_name,
            sport_key=signal_data.sport_key,
            hours_before=6,
        )

        # ── 3. Sentiment en parallèle ────────────────────────────
        sentiment_task = self.sentiment.analyze(
            sharp_prob=signal_data.sharp_prob,
            soft_prob=signal_data.soft_prob,
            soft_odds_home=sharp_odds[0] if sharp_odds else 0,
            soft_odds_away=sharp_odds[1] if len(sharp_odds) > 1 else 0,
        )

        # Exécution parallèle
        vpin_result, news_result, sentiment_result = await asyncio.gather(
            vpin_task, news_task, sentiment_task,
            return_exceptions=True,
        )

        # ── Synthèse ─────────────────────────────────────────────
        signal_data.vpin_toxicity = vpin_result.toxicity if isinstance(vpin_result, VPINResult) else "unknown"
        signal_data.has_critical_injury = news_result.get("has_critical_injury", False) if isinstance(news_result, dict) else False
        signal_data.injuries = news_result.get("injuries", []) if isinstance(news_result, dict) else []
        signal_data.news_headlines = news_result.get("headlines", []) if isinstance(news_result, dict) else []

        if isinstance(sentiment_result, dict):
            signal_data.sentiment_public = sentiment_result.get("sentiment", "neutral")
            signal_data.sentiment_score = sentiment_result.get("irrationality_score", 0.0)

        # ── Application de la Doctrine ───────────────────────────
        # Règle 1 : Blessure critique → REJET
        if signal_data.has_critical_injury:
            signal_data.recommended_action = "reject"
            logger.warning(f"❌ Rejet contextuel: blessure critique sur {signal_data.event_name}")
            return signal_data

        # Règle 2 : VPIN informed + sentiment irrationnel → OPPORTUNITÉ
        if (signal_data.vpin_toxicity == "informed"
                and signal_data.sentiment_public == "irrational"
                and signal_data.sentiment_score > 0.05):
            signal_data.recommended_action = "bet"
            signal_data.public_against = True
            logger.info(f"🎯 Opportunité: VPIN informed + foule irrationnelle sur {signal_data.event_name}")
            return signal_data

        # Règle 3 : VPIN noise + pas de news critique → HOLD
        if signal_data.vpin_toxicity == "noise" and not signal_data.has_critical_injury:
            signal_data.recommended_action = "hold"
            return signal_data

        # Règle 4 : VPIN unknown avec écart significatif → BET prudent
        if signal_data.ev_plus >= 0.08 and signal_data.vpin_toxicity != "noise":
            signal_data.recommended_action = "bet"
            return signal_data

        # Règle 5 : VPIN informed avec confirmation → BET
        if signal_data.vpin_toxicity == "informed":
            signal_data.recommended_action = "bet"
            return signal_data

        signal_data.recommended_action = "hold"
        return signal_data

    def get_queue_stats(self) -> dict:
        return self.queue.get_stats()


# Singleton
context_analyzer = ContextAnalyzer()