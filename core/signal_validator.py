"""
core/signal_validator.py v2.1
CORRECTIF : Gemini 429 → fallback automatique Groq
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Optional

from config import settings
from core.paim_engine import PAIMSignal

logger = logging.getLogger(__name__)


@dataclass
class ValidationResult:
    is_approved: bool
    confidence: float
    context_summary: str
    risk_flags: list[str]
    trap_detected: bool


SYSTEM_PROMPT = """Tu es un analyste quantitatif spécialisé en marchés de paris sportifs.
Tu reçois un signal EV+ et tu dois détecter les informations invalidantes.

Réponds UNIQUEMENT en JSON strict :
{
  "is_approved": true/false,
  "confidence": 0.0-1.0,
  "context_summary": "résumé 1 phrase",
  "risk_flags": [],
  "trap_detected": false,
  "trap_reason": ""
}

Critères de rejet : blessure joueur clé <6h avant match, météo extrême,
cote en mouvement contre notre sélection dans les 30 dernières minutes."""


def _parse_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        parts = text.split("```")
        text = parts[1] if len(parts) > 1 else text
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text.strip())


class GeminiValidator:
    """Validateur IA avec fallback Groq si quota Gemini épuisé."""

    def __init__(self):
        self._gemini = None
        self._groq = None
        self._gemini_cooldown_until: float = 0

    def _get_gemini(self):
        if self._gemini is None:
            import google.generativeai as genai
            genai.configure(api_key=settings.gemini_api_key)
            self._gemini = genai.GenerativeModel(
                model_name="gemini-2.0-flash",
                system_instruction=SYSTEM_PROMPT,
            )
        return self._gemini

    def _get_groq(self):
        if self._groq is None:
            from groq import Groq
            self._groq = Groq(api_key=settings.groq_api_key)
        return self._groq

    def _prompt(self, signal: PAIMSignal, event_name: str,
                sport: str, match_time: str, news: Optional[str]) -> str:
        return f"""
SIGNAL À VALIDER :
- Événement : {event_name} ({sport})
- Heure : {match_time}
- Sélection : {signal.selection.upper()}
- EV+ : {signal.ev_plus:.2%}
- SNR : {signal.snr_ratio:.2f}
- Prob. Sharp : {signal.sharp_prob:.3f}
- Prob. Soft  : {signal.implied_prob_soft:.3f}

CONTEXTE :
{news or "Aucune news détectée."}

Retourne uniquement le JSON d'évaluation.
"""

    async def validate(
        self,
        signal: PAIMSignal,
        event_name: str,
        sport: str,
        match_time_iso: str,
        recent_news: Optional[str] = None,
    ) -> ValidationResult:

        prompt = self._prompt(signal, event_name, sport, match_time_iso, recent_news)

        # ── 1. Tentative Gemini ────────────────────────────────
        if time.time() > self._gemini_cooldown_until and settings.gemini_api_key:
            try:
                resp = self._get_gemini().generate_content(prompt)
                data = _parse_json(resp.text)
                return ValidationResult(
                    is_approved=data.get("is_approved", False),
                    confidence=float(data.get("confidence", 0.0)),
                    context_summary=data.get("context_summary", ""),
                    risk_flags=data.get("risk_flags", []),
                    trap_detected=data.get("trap_detected", False),
                )
            except Exception as e:
                if "429" in str(e) or "quota" in str(e).lower():
                    # Cooldown 60 min avant de réessayer Gemini
                    self._gemini_cooldown_until = time.time() + 3600
                    logger.warning("Gemini 429 → cooldown 60min → fallback Groq")
                else:
                    logger.error(f"Gemini error: {e}")

        # ── 2. Fallback Groq ───────────────────────────────────
        if settings.groq_api_key:
            try:
                completion = self._get_groq().chat.completions.create(
                    model=settings.groq_model,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user",   "content": prompt},
                    ],
                    temperature=0.1,
                    max_tokens=300,
                )
                data = _parse_json(completion.choices[0].message.content)
                return ValidationResult(
                    is_approved=data.get("is_approved", False),
                    confidence=float(data.get("confidence", 0.0)),
                    context_summary=f"[Groq] {data.get('context_summary', '')}",
                    risk_flags=data.get("risk_flags", []),
                    trap_detected=data.get("trap_detected", False),
                )
            except Exception as e:
                logger.error(f"Groq fallback error: {e}")

        # ── 3. Double panne → approbation conservative ─────────
        logger.warning("Tous les validateurs IA down — signal conservé")
        return ValidationResult(
            is_approved=True,
            confidence=0.5,
            context_summary="Validation IA indisponible — signal conservé.",
            risk_flags=["ai_unavailable"],
            trap_detected=False,
        )