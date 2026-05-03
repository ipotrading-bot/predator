"""
core/signal_validator.py — Validation contextuelle via Gemini 2.0 Flash
Vérifie: news d'équipe, météo, draft e-sport, compositions
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Optional

import google.generativeai as genai

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


class GeminiValidator:
    """
    Utilise Gemini 2.0 Flash pour valider le contexte d'un signal PAIM.
    Détecte les Trap Lines et les news invalidantes.
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
        genai.configure(api_key=settings.gemini_api_key)
        self.model = genai.GenerativeModel(
            model_name="gemini-2.0-flash",
            system_instruction=self.SYSTEM_PROMPT,
        )

    async def validate(
        self,
        signal: PAIMSignal,
        event_name: str,
        sport: str,
        match_time_iso: str,
        recent_news: Optional[str] = None,
    ) -> ValidationResult:
        """Valide un signal PAIM avec le contexte Gemini."""
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
            logger.error(f"Gemini validation error: {e}")
            return ValidationResult(
                is_approved=False,
                confidence=0.0,
                context_summary="Erreur de validation IA — signal rejeté par précaution.",
                risk_flags=["gemini_error"],
                trap_detected=False,
            )
