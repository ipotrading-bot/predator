"""
core/signal_validator.py — Validation contextuelle via Groq (primaire) + Gemini (fallback)
Vérifie: news d'équipe, météo, draft e-sport, compositions
Priorité: Groq pour la vitesse (LPU), Gemini en repli
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Optional

import google.generativeai as genai
from groq import Groq

from config import settings
from core.paim_engine import PAIMSignal

logger = logging.getLogger(__name__)


@dataclass
class ValidationResult:
    is_approved: bool
    confidence: float       # 0.0 à 1.0
    context_summary: str
    risk_flags: list[str]
    trap_detected: bool


class GroqValidator:
    """
    Utilise Groq (LPU) pour validation ultra-rapide des signaux PAIM.
    10x plus rapide que Gemini pour le trading haute fréquence.
    """

    SYSTEM_PROMPT = """Tu es un analyste quantitatif spécialisé en marchés de paris sportifs.
    
    Tu reçois un signal de valeur (EV+) issu d'une analyse algorithmique.
    Ta mission : analyser le CONTEXTE pour détecter des informations invalidantes.
    
    Réponds UNIQUEMENT en JSON avec ce format exact :
    {
        "is_approved": true/false,
        "confidence": 0.0-1.0,
        "context_summary": "résumé en 1 phrase",
        "risk_flags": ["flag1", "flag2"],
        "trap_detected": true/false,
        "trap_reason": "raison si trap détecté"
    }
    
    Critères de rejet :
    - Blessure ou suspension d'un joueur clé (< 6h avant match)
    - Conditions météo extrêmes (football extérieur)
    - Draft e-sport avec pick atypique
    - Volume de paris anormalement élevé en sens inverse du signal
    - Cote en mouvement CONTRE notre sélection dans les 30 dernières minutes
    """

    def __init__(self):
        if not settings.groq_api_key:
            self.client = None
            self.enabled = False
            return

        self.client = Groq(api_key=settings.groq_api_key)
        self.enabled = True
        self.model = settings.groq_model

    async def validate(
        self,
        signal: PAIMSignal,
        event_name: str,
        sport: str,
        match_time_iso: str,
        recent_news: Optional[str] = None,
    ) -> ValidationResult:
        """Valide un signal PAIM avec Groq (rapide)."""
        if not self.enabled:
            return self._default_rejection("Groq disabled")

        # Fetch news if not provided
        if recent_news is None:
            recent_news = await self._fetch_news_context(event_name, sport)

        prompt = self._build_prompt(signal, event_name, sport, match_time_iso, recent_news)

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": self.SYSTEM_PROMPT},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.1,
                max_tokens=200,
            )

            raw = response.choices[0].message.content.strip()
            return self._parse_response(raw)

        except Exception as e:
            logger.warning(f"Groq validation error: {e}")
            return None  # Signal to fallback to Gemini

    def _build_prompt(self, signal, event_name, sport, match_time_iso, recent_news):
        return f"""
SIGNAL À VALIDER :
- Événement : {event_name} ({sport})
- Heure du match : {match_time_iso}
- Sélection : {signal.selection.upper()}
- EV+ : {signal.ev_plus:.2%}
- SNR : {signal.snr_ratio:.2f}
- Probabilité Sharp (Pinnacle) : {signal.sharp_prob:.3f}
- Probabilité implicite Soft : {signal.implied_prob_soft:.3f}
- CLV estimée : {signal.clv_estimate:.2%}

CONTEXTE DISPONIBLE :
{recent_news or "Aucune news spécifique détectée."}

Analyse ce signal et retourne ton évaluation JSON.
"""

    def _parse_response(self, raw: str) -> ValidationResult:
        try:
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            raw = raw.strip()

            data = json.loads(raw)
            return ValidationResult(
                is_approved=data.get("is_approved", False),
                confidence=float(data.get("confidence", 0.0)),
                context_summary=data.get("context_summary", ""),
                risk_flags=data.get("risk_flags", []),
                trap_detected=data.get("trap_detected", False),
            )
        except (json.JSONDecodeError, KeyError, Exception) as e:
            logger.error(f"Groq parse error: {e}")
            return self._default_rejection("parse_error")

    def _default_rejection(self, reason: str) -> ValidationResult:
        return ValidationResult(
            is_approved=False,
            confidence=0.0,
            context_summary=f"Erreur de validation — {reason}",
            risk_flags=[reason],
            trap_detected=False,
        )

    async def _fetch_news_context(self, event_name: str, sport: str) -> str:
        """Récupère le contexte news pour un événement donné."""
        try:
            from api.news_client import news_client
            import asyncio

            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            news_items = loop.run_until_complete(
                news_client.get_relevant_news(sport, None, hours=6)
            )
            loop.close()

            if news_items:
                news_text = "\n".join([
                    f"- {item['title']} ({item.get('source', 'N/A')})"
                    for item in news_items[:3]
                ])
                return news_text
        except ImportError:
            pass
        except Exception as e:
            logger.debug(f"News fetch error: {e}")

        return "Aucune news spécifique détectée."


class GeminiValidator:
    """
    Fallback: Utilise Gemini 2.0 Flash pour valider le contexte d'un signal PAIM.
    Plus lent que Groq mais plus puissant pour l'analyse contextuelle.
    """

    SYSTEM_PROMPT = GroqValidator.SYSTEM_PROMPT  # Same prompt

    def __init__(self):
        if not settings.gemini_api_key:
            self.model = None
            self.enabled = False
            return

        genai.configure(api_key=settings.gemini_api_key)
        self.model = genai.GenerativeModel(
            model_name="gemini-2.0-flash",
            system_instruction=self.SYSTEM_PROMPT,
        )
        self.enabled = True

    async def validate(
        self,
        signal: PAIMSignal,
        event_name: str,
        sport: str,
        match_time_iso: str,
        recent_news: Optional[str] = None,
    ) -> ValidationResult:
        """Valide un signal PAIM avec Gemini (fallback)."""
        if not self.enabled:
            return self._default_rejection("Gemini disabled")

        if recent_news is None:
            recent_news = await self._fetch_news_context(event_name, sport)

        prompt = f"""
SIGNAL À VALIDER :
- Événement : {event_name} ({sport})
- Heure du match : {match_time_iso}
- Sélection : {signal.selection.upper()}
- EV+ : {signal.ev_plus:.2%}
- SNR : {signal.snr_ratio:.2f}
- Probabilité Sharp (Pinnacle) : {signal.sharp_prob:.3f}
- Probabilité implicite Soft : {signal.implied_prob_soft:.3f}
- CLV estimée : {signal.clv_estimate:.2%}

CONTEXTE DISPONIBLE :
{recent_news or "Aucune news spécifique détectée."}

Analyse ce signal et retourne ton évaluation JSON.
"""
        try:
            response = self.model.generate_content(prompt)
            raw = response.text.strip()
            return self._parse_response(raw)

        except Exception as e:
            logger.error(f"Gemini validation error: {e}")
            return self._default_rejection("gemini_error")

    def _parse_response(self, raw: str) -> ValidationResult:
        try:
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            raw = raw.strip()

            data = json.loads(raw)
            return ValidationResult(
                is_approved=data.get("is_approved", False),
                confidence=float(data.get("confidence", 0.0)),
                context_summary=data.get("context_summary", ""),
                risk_flags=data.get("risk_flags", []),
                trap_detected=data.get("trap_detected", False),
            )
        except (json.JSONDecodeError, KeyError, Exception) as e:
            logger.error(f"Gemini parse error: {e}")
            return self._default_rejection("parse_error")

    def _default_rejection(self, reason: str) -> ValidationResult:
        return ValidationResult(
            is_approved=False,
            confidence=0.0,
            context_summary=f"Erreur de validation — {reason}",
            risk_flags=[reason],
            trap_detected=False,
        )

    async def _fetch_news_context(self, event_name: str, sport: str) -> str:
        """Récupère le contexte news pour un événement donné."""
        try:
            from api.news_client import news_client
            import asyncio

            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            news_items = loop.run_until_complete(
                news_client.get_relevant_news(sport, None, hours=6)
            )
            loop.close()

            if news_items:
                news_text = "\n".join([
                    f"- {item['title']} ({item.get('source', 'N/A')})"
                    for item in news_items[:3]
                ])
                return news_text
        except ImportError:
            pass
        except Exception as e:
            logger.debug(f"News fetch error: {e}")

        return "Aucune news spécifique détectée."


class SignalValidator:
    """
    Orchestrateur de validation: Groq en premier, Gemini en fallback.
    """

    def __init__(self):
        self.groq = GroqValidator()
        self.gemini = GeminiValidator()

    async def validate(
        self,
        signal: PAIMSignal,
        event_name: str,
        sport: str,
        match_time_iso: str,
        recent_news: Optional[str] = None,
    ) -> ValidationResult:
        """
        Valide un signal PAIM avec Groq (rapide) + Gemini (fallback).
        
        Stratégie:
        1. Essayer Groq en premier (ultra-rapide, <100ms)
        2. Si Groq échoue ou n'est pas disponible, fallback sur Gemini
        3. Si les deux échouent, rejeter par précaution
        """
        # Try Groq first (fast path)
        if self.groq.enabled:
            result = await self.groq.validate(
                signal, event_name, sport, match_time_iso, recent_news
            )
            if result is not None:
                return result
            logger.info("Groq failed, falling back to Gemini")

        # Fallback to Gemini
        if self.gemini.enabled:
            result = await self.gemini.validate(
                signal, event_name, sport, match_time_iso, recent_news
            )
            if result is not None:
                return result
            logger.warning("Gemini also failed")

        # Both failed - reject by default
        return ValidationResult(
            is_approved=False,
            confidence=0.0,
            context_summary="Échec des deux validateurs (Groq + Gemini)",
            risk_flags=["groq_failed", "gemini_failed"],
            trap_detected=False,
        )


# Singleton
signal_validator = SignalValidator()