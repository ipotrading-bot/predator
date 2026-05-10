"""
api/groq_client.py — Groq AI Client for Ultra-Fast Signal Filtering
Utilise Groq (LPU) pour le filtrage bayésien de premier niveau.
Groq est 10x plus rapide que Gemini pour l'inférence, idéal pour le trading haute fréquence.
"""
from __future__ import annotations

import json
import time
from typing import Optional

from config import settings


class GroqClient:
    """Client Groq pour filtrage rapide des signaux."""
    
    def __init__(self):
        if not settings.groq_api_key:
            self.client = None
            self.enabled = False
            return

        from groq import Groq
        self.client = Groq(api_key=settings.groq_api_key)
        self.enabled = True
        self.model = settings.groq_model
        self._request_count = 0
        self._total_latency_ms = 0
    
    async def quick_filter(self, signal_data: dict) -> dict:
        """
        Filtrage bayésien ultra-rapide avec Groq.
        Retourne une décision en < 100ms.
        
        Args:
            signal_data: Dict contenant les infos du signal
                - ev_plus: Valeur attendue positive (%)
                - sharp_prob: Probabilité sharp
                - implied_prob: Probabilité implicite
                - sport: Sport concerné
                - market: Type de marché
        
        Returns:
            dict: {"approved": bool, "confidence": float, "reason": str, "latency_ms": int}
        """
        if not self.enabled:
            return {
                "approved": True,  # Fallback: approuver par défaut
                "confidence": 0.5,
                "reason": "Groq disabled, using fallback",
                "latency_ms": 0
            }
        
        start_time = time.monotonic()
        
        try:
            prompt = self._build_filter_prompt(signal_data)
            
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": self._get_system_prompt()
                    },
                    {
                        "role": "user",
                        "content": prompt
                    }
                ],
                temperature=0.1,  # Low temperature for consistent filtering
                max_tokens=100,
            )
            
            latency_ms = int((time.monotonic() - start_time) * 1000)
            self._request_count += 1
            self._total_latency_ms += latency_ms
            
            # Parse response
            result = self._parse_response(response.choices[0].message.content)
            result["latency_ms"] = latency_ms
            
            return result
            
        except Exception as e:
            return {
                "approved": True,  # Fallback: approuver en cas d'erreur
                "confidence": 0.3,
                "reason": f"Groq error: {str(e)}",
                "latency_ms": int((time.monotonic() - start_time) * 1000)
            }
    
    def batch_filter(self, signals: list[dict]) -> list[dict]:
        """
        Filtrage en batch pour plusieurs signaux.
        Plus efficient que les appels individuels.
        """
        if not self.enabled or not signals:
            return [{"approved": True, "confidence": 0.5} for _ in signals]
        
        results = []
        for signal in signals:
            result = self._sync_quick_filter(signal)
            results.append(result)
        
        return results
    
    def _sync_quick_filter(self, signal_data: dict) -> dict:
        """Version synchrone pour batch processing."""
        start_time = time.monotonic()
        
        try:
            prompt = self._build_filter_prompt(signal_data)
            
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": self._get_system_prompt()
                    },
                    {
                        "role": "user",
                        "content": prompt
                    }
                ],
                temperature=0.1,
                max_tokens=100,
            )
            
            latency_ms = int((time.monotonic() - start_time) * 1000)
            self._request_count += 1
            self._total_latency_ms += latency_ms
            
            result = self._parse_response(response.choices[0].message.content)
            result["latency_ms"] = latency_ms
            
            return result
            
        except Exception as e:
            return {
                "approved": True,
                "confidence": 0.3,
                "reason": f"Groq error: {str(e)}",
                "latency_ms": int((time.monotonic() - start_time) * 1000)
            }
    
    def get_stats(self) -> dict:
        """Retourne les statistiques de performance Groq."""
        avg_latency = (
            self._total_latency_ms / self._request_count 
            if self._request_count > 0 
            else 0
        )
        
        return {
            "enabled": self.enabled,
            "model": self.model,
            "total_requests": self._request_count,
            "avg_latency_ms": round(avg_latency, 2),
            "total_latency_ms": self._total_latency_ms
        }
    
    @staticmethod
    def _get_system_prompt() -> str:
        """Prompt système pour le filtrage bayésien."""
        return """Tu es un filtre bayésien ultra-rapide pour un système de trading sportif (PAIM).
Ton rôle: analyser les signaux de valeur (EV+) et décider s'ils doivent être validés ou rejetés.

Critères de rejet:
- EV+ < 1.0%: En dessous du seuil de détection
- Anomalie manifeste dans les cotes (cote < 1.01 ou > 50)
- Incohérence flagrante entre sharp_prob et implied_prob (écart > 50%)

Format de réponse JSON STRICT (pas de texte autour):
{
    "approved": true/false,
    "confidence": 0.0-1.0,
    "reason": "raison courte en français",
    "risk_level": "LOW/MEDIUM/HIGH"
}

Sois CONSERVATEUR. Mieux vaut rejeter un bon signal qu'accepter un mauvais."""
    
    def _build_filter_prompt(self, signal_data: dict) -> str:
        """Construit le prompt pour l'analyse du signal."""
        return f"""Analyse ce signal de trading sportif:

Sport: {signal_data.get('sport', 'N/A')}
Marché: {signal_data.get('market_key', 'N/A')}
Sélection: {signal_data.get('selection', 'N/A')}
EV+ (Edge): {signal_data.get('ev_plus', 0)}%
Sharp Probability: {signal_data.get('sharp_prob', 0)}%
Implied Probability: {signal_data.get('implied_prob_soft', 0)}%
SNR Ratio: {signal_data.get('snr_ratio', 0)}
Mise recommandée: {signal_data.get('recommended_stake', 0)}€

Décision?"""
    
    @staticmethod
    def _parse_response(response_text: str) -> dict:
        """Parse la réponse JSON de Groq."""
        try:
            # Nettoyer la réponse (parfois Groq ajoute du texte)
            cleaned = response_text.strip()
            if cleaned.startswith("```json"):
                cleaned = cleaned[7:]
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3]
            cleaned = cleaned.strip()
            
            result = json.loads(cleaned)
            return {
                "approved": result.get("approved", False),
                "confidence": float(result.get("confidence", 0)),
                "reason": result.get("reason", "Unknown"),
                "risk_level": result.get("risk_level", "MEDIUM")
            }
        except (json.JSONDecodeError, ValueError):
            return {
                "approved": False,
                "confidence": 0.0,
                "reason": "Parse error in Groq response",
                "risk_level": "HIGH"
            }


# Singleton
groq_client = GroqClient()