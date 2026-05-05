"""
api/perplexity_client.py — Perplexity Sonar API for Real-Time Grounding
Couche de vérification factuelle : élimine les hallucinations des LLM.
Interroge Perplexity pour confirmer l'état des blessures/lineups en < 10 minutes.

Doctrine PhD MIT : Perplexity confirme les faits. Pinnacle confirme la valeur.
Si Perplexity et Pinnacle sont alignés → signal BET. Sinon → REJET.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Optional

from config import settings

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════
# STRUCTURES DE DONNÉES
# ═══════════════════════════════════════════════════════════════════

class GroundingResult:
    """Résultat de la vérification factuelle Perplexity."""
    
    def __init__(
        self,
        verified: bool = False,
        confidence: float = 0.0,
        summary: str = "",
        sources: list[str] = None,
        contradictions: list[str] = None,
        latency_ms: int = 0,
    ):
        self.verified = verified
        self.confidence = confidence
        self.summary = summary
        self.sources = sources or []
        self.contradictions = contradictions or []
        self.latency_ms = latency_ms

    @property
    def is_reliable(self) -> bool:
        """Vérifié avec confiance > 0.7 et pas de contradictions majeures."""
        return self.verified and self.confidence >= 0.7 and len(self.contradictions) < 2

    def to_dict(self) -> dict:
        return {
            "verified": self.verified,
            "confidence": self.confidence,
            "summary": self.summary,
            "sources": self.sources[:3],
            "contradictions": self.contradictions[:3],
            "latency_ms": self.latency_ms,
            "is_reliable": self.is_reliable,
        }


# ═══════════════════════════════════════════════════════════════════
# PERPLEXITY CLIENT — Grounding en temps réel
# ═══════════════════════════════════════════════════════════════════

class PerplexityGrounding:
    """
    Client Perplexity Sonar pour la vérification factuelle en temps réel.
    
    Utilisation :
    1. Le bot détecte un signal PAIM potentiel
    2. Perplexity vérifie : "Confirme l'état de la cheville de LeBron James"
    3. Si blessure confirmée → REJET
    4. Si joueur confirmé présent → maintient le signal
    
    Fallback : si pas de clé API, utilise heuristiques basées sur NewsAPI.
    """
    
    # Prompts spécialisés par type de vérification
    GROUNDING_TEMPLATES = {
        "injury": (
            "Search the web and confirm the injury status of {player} for the upcoming "
            "game on {date}. Is {player} ruled OUT, QUESTIONABLE, PROBABLE, or confirmed "
            "to play? Provide a definitive answer with sources from the last 24 hours only."
        ),
        "lineup": (
            "Search the web for the confirmed starting lineup of {team} in their next match "
            "on {date}. List any key players who are benched or absent. Sources must be from "
            "the last 6 hours."
        ),
        "weather": (
            "Search the web for the current weather forecast for {location} on {date}. "
            "Is there any extreme weather (rain, snow, wind) that could affect an outdoor "
            "sports event? Be specific about expected conditions."
        ),
        "general": (
            "Search the web for the latest news about {query}. Focus on developments in the "
            "last 24 hours that could affect a sports betting line. Provide a concise summary "
            "with verifiable sources."
        ),
    }
    
    # Système prompt pour Perplexity Sonar
    SYSTEM_PROMPT = """You are a real-time sports fact-checker for the PREDATOR PAIM system.
Your job is to verify claims with CURRENT, VERIFIABLE sources only.

Rules:
1. ONLY use information from the last 24 hours
2. Cite specific sources (ESPN, official team accounts, beat reporters)
3. If information is uncertain, state it explicitly
4. Flag any contradictions between sources
5. NEVER rely on predictive models or opinions — only confirmed facts

Respond in JSON only:
{
    "verified": true/false,
    "confidence": 0.0-1.0,
    "summary": "one-line summary",
    "sources": ["source1", "source2"],
    "contradictions": ["contradiction if any"],
    "player_status": "OUT/PROBABLE/QUESTIONABLE/AVAILABLE/UNKNOWN"
}"""
    
    def __init__(self):
        self.api_key = settings.perplexity_api_key or None  # Perplexity API key
        self.model = "sonar-small-online"  # sonar-small-online ou sonar-pro
        self._request_count = 0
        self._total_latency_ms = 0
        self._cache = {}
        self._cache_ttl = 120  # 2 minutes de cache pour les vérifications
        
    def is_available(self) -> bool:
        """Vérifie si Perplexity est configuré."""
        return bool(self.api_key)
    
    async def verify_injury(
        self,
        player_name: str,
        team: str = "",
        event_date: str = "",
    ) -> GroundingResult:
        """
        Vérifie l'état d'une blessure en temps réel via Perplexity.
        
        Args:
            player_name: Nom du joueur à vérifier
            team: Équipe du joueur (optionnel)
            event_date: Date de l'événement (YYYY-MM-DD)
            
        Returns:
            GroundingResult avec le statut vérifié
        """
        cache_key = f"injury_{player_name}_{team}_{event_date}"
        cached = self._get_cached(cache_key)
        if cached:
            return cached
            
        if not self.is_available():
            # Fallback informatif
            return GroundingResult(
                verified=False,
                confidence=0.3,
                summary=f"Perplexity non configuré — statut {player_name} non vérifié.",
                latency_ms=0,
            )
        
        query = self.GROUNDING_TEMPLATES["injury"].format(
            player=player_name,
            date=event_date or "today",
        )
        
        return await self._ground(query, cache_key)
    
    async def verify_lineup(
        self,
        team: str,
        event_date: str = "",
    ) -> GroundingResult:
        """
        Vérifie la composition d'équipe via Perplexity.
        """
        cache_key = f"lineup_{team}_{event_date}"
        cached = self._get_cached(cache_key)
        if cached:
            return cached
            
        if not self.is_available():
            return GroundingResult(
                verified=False,
                confidence=0.3,
                summary=f"Perplexity non configuré — lineup {team} non vérifié.",
                latency_ms=0,
            )
        
        query = self.GROUNDING_TEMPLATES["lineup"].format(
            team=team,
            date=event_date or "today",
        )
        
        return await self._ground(query, cache_key)
    
    async def verify_factual_claim(
        self,
        query_text: str,
        context_type: str = "general",
    ) -> GroundingResult:
        """
        Vérification générique d'une affirmation factuelle.
        
        Args:
            query_text: Texte de la requête de vérification
            context_type: Type de contexte (injury, lineup, weather, general)
            
        Returns:
            GroundingResult résultat de vérification
        """
        cache_key = f"fact_{hash(query_text)}_{context_type}"
        cached = self._get_cached(cache_key)
        if cached:
            return cached
            
        if not self.is_available():
            template = self.GROUNDING_TEMPLATES.get(context_type, self.GROUNDING_TEMPLATES["general"])
            query = template.format(query=query_text)
            return await self._fallback_grounding(query)
        
        template = self.GROUNDING_TEMPLATES.get(context_type, self.GROUNDING_TEMPLATES["general"])
        query = template.format(query=query_text)
        
        return await self._ground(query, cache_key)
    
    async def _ground(self, query: str, cache_key: str) -> GroundingResult:
        """Exécute la requête Perplexity et parse la réponse."""
        start = time.monotonic()
        
        try:
            # Tentative avec httpx (fallback si pas de lib Perplexity officielle)
            import httpx
            
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.post(
                    "https://api.perplexity.ai/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self.model,
                        "messages": [
                            {"role": "system", "content": self.SYSTEM_PROMPT},
                            {"role": "user", "content": query},
                        ],
                        "temperature": 0.1,
                        "max_tokens": 300,
                    },
                )
                
                if response.status_code != 200:
                    logger.warning(f"Perplexity API error: {response.status_code}")
                    latency = int((time.monotonic() - start) * 1000)
                    return self._error_result("API error", latency)
                
                data = response.json()
                content = data["choices"][0]["message"]["content"]
                latency_ms = int((time.monotonic() - start) * 1000)
                
                self._request_count += 1
                self._total_latency_ms += latency_ms
                
                # Parse JSON response
                result = self._parse_response(content)
                result.latency_ms = latency_ms
                
                # Cache
                self._set_cache(cache_key, result)
                
                return result
                
        except ImportError:
            # httpx pas disponible — fallback
            latency = int((time.monotonic() - start) * 1000)
            return await self._fallback_grounding(query)
            
        except Exception as e:
            logger.error(f"Perplexity grounding error: {e}")
            latency = int((time.monotonic() - start) * 1000)
            return self._error_result(str(e), latency)
    
    async def _fallback_grounding(self, query: str) -> GroundingResult:
        """
        Fallback quand Perplexity n'est pas disponible.
        Utilise NewsAPI comme source de vérification alternative.
        """
        try:
            from core.context import ContextNewsAPI
            news = ContextNewsAPI()
            context = await news.fetch_context(
                event_name=query[:100],
                sport_key="",
                hours_before=6,
            )
            
            if context.get("has_critical_injury"):
                return GroundingResult(
                    verified=True,
                    confidence=0.6,
                    summary="Blessure critique détectée via NewsAPI (fallback).",
                    sources=[n["title"] for n in context.get("injuries", [])[:2]],
                    contradictions=[],
                    latency_ms=0,
                )
            
            return GroundingResult(
                verified=False,
                confidence=0.4,
                summary="Fallback NewsAPI — vérification limitée sans Perplexity.",
                sources=context.get("headlines", [])[:2],
                contradictions=[],
                latency_ms=0,
            )
            
        except Exception:
            return self._error_result("Fallback échoué", 0)
    
    def _parse_response(self, content: str) -> GroundingResult:
        """Parse la réponse JSON de Perplexity."""
        try:
            # Nettoyage
            cleaned = content.strip()
            if cleaned.startswith("```json"):
                cleaned = cleaned[7:]
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3]
            cleaned = cleaned.strip()
            
            data = json.loads(cleaned)
            
            return GroundingResult(
                verified=data.get("verified", False),
                confidence=float(data.get("confidence", 0)),
                summary=data.get("summary", ""),
                sources=data.get("sources", []),
                contradictions=data.get("contradictions", []),
            )
            
        except (json.JSONDecodeError, ValueError):
            # Fallback: extraction regex simple
            import re
            verified = "true" in content.lower()[:100]
            confidence = 0.5 if verified else 0.3
            
            return GroundingResult(
                verified=verified,
                confidence=confidence,
                summary=content[:200],
                sources=[],
                contradictions=[],
            )
    
    def _error_result(self, error_msg: str, latency_ms: int) -> GroundingResult:
        return GroundingResult(
            verified=False,
            confidence=0.0,
            summary=f"Erreur vérification: {error_msg}",
            latency_ms=latency_ms,
        )
    
    def _get_cached(self, key: str) -> Optional[GroundingResult]:
        if key in self._cache:
            ts, result = self._cache[key]
            if time.time() - ts < self._cache_ttl:
                return result
        return None
    
    def _set_cache(self, key: str, result: GroundingResult) -> None:
        self._cache[key] = (time.time(), result)
        # Nettoyage du cache si trop volumineux
        if len(self._cache) > 100:
            old_keys = sorted(
                self._cache.keys(),
                key=lambda k: self._cache[k][0],
            )[:50]
            for k in old_keys:
                del self._cache[k]
    
    def get_stats(self) -> dict:
        avg_latency = (
            self._total_latency_ms / self._request_count
            if self._request_count > 0
            else 0
        )
        return {
            "available": self.is_available(),
            "model": self.model,
            "total_requests": self._request_count,
            "avg_latency_ms": round(avg_latency, 2),
            "cache_size": len(self._cache),
        }


# Singleton
perplexity_grounding = PerplexityGrounding()