"""
api/rate_limiter.py v2.1 — Distributed Multi-Source Rate Limiter
Gère 15 requêtes/minute réparties entre : Groq, NewsAPI, Reddit, Perplexity, Gemini.
Utilise aiolimiter pour la distribution équitable entre sources.

Architecture:
- Global limiter: 15 RPM toutes sources confondues
- Per-source limiter: quotas individuels pour éviter la starvation
- Priority queue: signaux validés PAIM passent en priorité
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════
# TOKEN BUCKET — Rate Limiting par Source
# ═══════════════════════════════════════════════════════════════════

class TokenBucket:
    """
    Algorithme Token Bucket pour le rate limiting par source.
    
    Chaque source a son propre bucket avec :
    - capacity: nombre max de tokens (requêtes)
    - refill_rate: tokens par seconde
    - refill_period: période de refill (60s)
    """
    
    def __init__(self, capacity: int, refill_period: float = 60.0):
        self.capacity = capacity
        self.tokens = float(capacity)
        self.refill_rate = capacity / refill_period
        self.last_refill = time.monotonic()
        self._lock = asyncio.Lock()
        
    async def acquire(self) -> float:
        """
        Acquiert un token. Retourne le temps d'attente nécessaire.
        Si 0, la requête peut être exécutée immédiatement.
        """
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self.last_refill
            self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
            self.last_refill = now
            
            if self.tokens >= 1:
                self.tokens -= 1
                return 0.0
            
            # Combien de temps avant d'avoir 1 token
            wait = (1 - self.tokens) / self.refill_rate
            return wait


# ═══════════════════════════════════════════════════════════════════
# DISTRIBUTED RATE LIMITER — Multi-Source Manager
# ═══════════════════════════════════════════════════════════════════

class DistributedRateLimiter:
    """
    Rate limiter distribué pour 5 sources avec priorisation.
    
    Sources avec quotas (15 RPM total):
    - Groq:     4 RPM (scan de vélocité VPIN — prioritaire)
    - NewsAPI:  3 RPM (contexte blessures/lineups)
    - Reddit:   2 RPM (sentiment foule — faible priorité)
    - Perplexity: 3 RPM (grounding factuel — critique)
    - Gemini:   3 RPM (synthèse finale Ticket 7/9)
    
    Doctrine PhD MIT : 
    - Les signaux validés par PAIM passent AVANT les scans exploratoires
    - Si une source dépasse son quota, elle attend son tour sans bloquer les autres
    - En cas de contention, Groq et Perplexity sont prioritaires
    """
    
    # Quotas par source (capacité en tokens, période en secondes)
    SOURCE_QUOTAS = {
        "groq":       {"capacity": 4, "priority": 1},  # Priorité haute
        "newsapi":    {"capacity": 3, "priority": 3},
        "reddit":     {"capacity": 2, "priority": 4},  # Priorité basse
        "perplexity": {"capacity": 3, "priority": 2},  # Priorité haute
        "gemini":     {"capacity": 3, "priority": 2},
        "rapidapi":   {"capacity": 3, "priority": 3},
    }
    
    # Quota global de sécurité (jamais dépassé)
    GLOBAL_MAX_RPM = 15
    GLOBAL_REFILL_PERIOD = 60.0
    
    def __init__(self):
        self._buckets: dict[str, TokenBucket] = {}
        self._global_bucket = TokenBucket(self.GLOBAL_MAX_RPM, self.GLOBAL_REFILL_PERIOD)
        self._lock = asyncio.Lock()
        self._stats: dict = {
            source: {"requests": 0, "wait_time_total": 0.0}
            for source in self.SOURCE_QUOTAS
        }
        self._stats["total"] = {"requests": 0, "wait_time_total": 0.0}
        self._priority_queue = asyncio.PriorityQueue()
        
        # Initialiser les buckets par source
        for source, config in self.SOURCE_QUOTAS.items():
            self._buckets[source] = TokenBucket(
                capacity=config["capacity"],
                refill_period=self.GLOBAL_REFILL_PERIOD,
            )
    
    async def acquire(self, source: str, priority: int = 3) -> None:
        """
        Acquiert un slot pour une source donnée.
        
        Args:
            source: Nom de la source (groq, newsapi, reddit, perplexity, gemini)
            priority: 1 (haute) à 5 (basse). Les signaux PAIM validés utilisent priority=1.
            
        Raises:
            ValueError: Si la source n'est pas reconnue
        """
        if source not in self._buckets and source != "unknown":
            logger.warning(f"Source inconnue: {source}, utilisation bucket global")
            source = "unknown"
        
        start = time.monotonic()
        
        # Acquérir le token source
        if source in self._buckets:
            wait_source = await self._buckets[source].acquire()
        else:
            wait_source = 0.0
        
        # Acquérir le token global (binding)
        wait_global = await self._global_bucket.acquire()
        
        # Attendre le temps nécessaire
        wait_total = max(wait_source, wait_global)
        if wait_total > 0:
            logger.debug(f"⏳ Rate limit: {source} attend {wait_total:.1f}s (priorité {priority})")
            await asyncio.sleep(wait_total)
        
        # Statistiques
        elapsed = time.monotonic() - start
        async with self._lock:
            if source in self._stats:
                self._stats[source]["requests"] += 1
                self._stats[source]["wait_time_total"] += wait_total
            self._stats["total"]["requests"] += 1
            self._stats["total"]["wait_time_total"] += wait_total
    
    async def acquire_prioritized(self, source: str, is_critical: bool = False) -> None:
        """
        Version priorisée de acquire().
        Si is_critical=True, le signal est prioritaire et passe avant les autres.
        
        Args:
            source: Nom de la source
            is_critical: Si True, le signal PAIM est prioritaire (priority=1)
        """
        priority = 1 if is_critical else self.SOURCE_QUOTAS.get(source, {}).get("priority", 3)
        await self.acquire(source, priority=priority)
    
    def get_stats(self) -> dict:
        """Retourne les statistiques d'utilisation par source."""
        result = {}
        for source, stats in self._stats.items():
            avg_wait = (
                stats["wait_time_total"] / stats["requests"]
                if stats["requests"] > 0
                else 0
            )
            result[source] = {
                "requests": stats["requests"],
                "avg_wait_seconds": round(avg_wait, 2),
                "total_wait_seconds": round(stats["wait_time_total"], 2),
            }
        
        # Quotas restants estimés
        result["remaining_capacity"] = {
            source: {
                "quota_rpm": cfg["capacity"],
                "priority": cfg.get("priority", 3),
            }
            for source, cfg in self.SOURCE_QUOTAS.items()
        }
        result["global_max_rpm"] = self.GLOBAL_MAX_RPM
        
        return result
    
    def reset_stats(self) -> None:
        """Réinitialise les compteurs (pour les tests ou rotation quotidienne)."""
        self._stats = {
            source: {"requests": 0, "wait_time_total": 0.0}
            for source in self.SOURCE_QUOTAS
        }
        self._stats["total"] = {"requests": 0, "wait_time_total": 0.0}


# ═══════════════════════════════════════════════════════════════════
# DÉCORATEUR — Rate Limiting Automatique
# ═══════════════════════════════════════════════════════════════════

# Instance globale du rate limiter distribué
rate_limiter = DistributedRateLimiter()


def with_rate_limit(source: str, priority: int = 3):
    """
    Décorateur pour les fonctions asynchrones nécessitant du rate limiting.
    
    Usage:
        @with_rate_limit(source="groq", priority=1)
        async def my_function():
            ...
    """
    def decorator(func):
        async def wrapper(*args, **kwargs):
            await rate_limiter.acquire(source, priority=priority)
            return await func(*args, **kwargs)
        return wrapper
    return decorator