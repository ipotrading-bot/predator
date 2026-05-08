"""
core/gemini_search.py — Gemini 2.0 Flash avec Google Search Grounding
Fallback Oracle quand The-Odds-API est épuisée ou sans données.

Stratégie:
1. Search Grounding pour trouver cotes Pinnacle (sharp) et 1XBet (soft)
2. Parsing JSON structuré via Groq
3. Calcul Shin Method sur données récupérées
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Optional

from config import settings

logger = logging.getLogger(__name__)


@dataclass
class GeminiOddsResult:
    """Résultat de recherche de cotes via Gemini."""
    home_team: str
    away_team: str
    pinnacle_home: Optional[float] = None
    pinnacle_away: Optional[float] = None
    onexbet_home: Optional[float] = None
    onexbet_away: Optional[float] = None
    match_time: Optional[str] = None
    source: str = "gemini_search"
    confidence: float = 0.0


class GeminiOracle:
    """
    Oracle Gemini 2.0 Flash avec Search Grounding.
    Remplace The-Odds-API quand quotas épuisés ou pas de données.
    """

    def __init__(self):
        self.api_key = settings.gemini_api_key
        self.model = "gemini-2.0-flash-exp"
        self.base_url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"

    def _build_search_prompt(self, home_team: str, away_team: str, sport: str = "soccer") -> str:
        """
        Construit le prompt optimisé pour extraction de cotes.
        
        Args:
            home_team: Nom équipe à domicile
            away_team: Nom équipe extérieur
            sport: Type de sport (soccer, basketball, tennis)
        """
        return f"""You are a specialized odds extraction agent for arbitrage detection.

TASK: Find current betting odds for this {sport.upper()} match:
{home_team} vs {away_team}

SEARCH INSTRUCTIONS:
1. Use Google Search to find CURRENT odds (today, May 2026) from:
   - Pinnacle Sports (sharp bookmaker)
   - 1XBet (soft bookmaker)

2. Return ONLY a JSON object with this exact structure:
{{
    "match": "{home_team} vs {away_team}",
    "pinnacle": {{
        "home": 1.85,
        "away": 2.10,
        "draw": 3.40
    }},
    "onexbet": {{
        "home": 1.95,
        "away": 2.05,
        "draw": 3.25
    }},
    "match_time": "2026-05-08T20:00:00Z",
    "source_url": "https://...",
    "confidence": "high"
}}

IMPORTANT:
- Use the EXACT decimal odds format shown above
- Include match_time if available
- If a team has significantly better odds on one book, note it
- Confidence: high/medium/low based on how recent the data is
- If odds not found, return null values and explain why"""

    def search_odds(self, home_team: str, away_team: str, sport: str = "soccer") -> Optional[GeminiOddsResult]:
        """
        Recherche les cotes via Gemini Search Grounding.
        
        Returns:
            GeminiOddsResult avec cotes trouvées ou None si échec
        """
        try:
            import requests

            prompt = self._build_search_prompt(home_team, away_team, sport)

            payload = {
                "contents": [{
                    "role": "user",
                    "parts": [{"text": prompt}]
                }],
                "tools": [{
                    "google_search": {}  # Active Search Grounding
                }],
                "generationConfig": {
                    "temperature": 0.1,
                    "maxOutputTokens": 1024,
                    "responseMimeType": "text/plain"
                }
            }

            response = requests.post(
                f"{self.base_url}?key={self.api_key}",
                json=payload,
                timeout=30
            )

            if response.status_code != 200:
                logger.error(f"Gemini API error: {response.status_code} - {response.text[:200]}")
                return None

            result = response.json()
            
            # Extraction du texte de réponse
            candidates = result.get("candidates", [])
            if not candidates:
                logger.warning("Gemini: No candidates in response")
                return None

            content = candidates[0].get("content", {})
            parts = content.get("parts", [])
            if not parts:
                return None

            raw_text = parts[0].get("text", "")
            
            # Log des sources utilisées (Grounding)
            grounding_chunks = result.get("groundingMetadata", {}).get("groundingChunks", [])
            sources = [chunk.get("web", {}).get("uri", "") for chunk in grounding_chunks]
            logger.info(f"🔍 Gemini Search sources: {sources[:2]}")

            # Parsing JSON depuis le texte
            return self._parse_odds_from_text(raw_text, home_team, away_team)

        except Exception as e:
            logger.error(f"Erreur Gemini Search: {e}")
            return None

    def _parse_odds_from_text(self, text: str, home: str, away: str) -> Optional[GeminiOddsResult]:
        """
        Parse le JSON des cotes depuis la réponse texte de Gemini.
        Fallback regex si JSON malformé.
        """
        result = GeminiOddsResult(home_team=home, away_team=away)

        try:
            # Essayer d'extraire JSON structuré
            json_match = re.search(r'\{[\s\S]*"pinnacle"[\s\S]*\}', text)
            if json_match:
                data = json.loads(json_match.group())
                
                result.pinnacle_home = self._extract_odd(data, ["pinnacle", "home"])
                result.pinnacle_away = self._extract_odd(data, ["pinnacle", "away"])
                result.onexbet_home = self._extract_odd(data, ["onexbet", "home"])
                result.onexbet_away = self._extract_odd(data, ["onexbet", "away"])
                result.match_time = data.get("match_time")
                result.confidence = 0.8 if data.get("confidence") == "high" else 0.5
                
                logger.info(f"✅ Gemini odds parsed: Pinnacle {result.pinnacle_home}/{result.pinnacle_away}, 1XBet {result.onexbet_home}/{result.onexbet_away}")
                return result

            # Fallback: Regex extraction
            logger.debug("JSON parsing failed, trying regex fallback...")
            
            # Chercher patterns comme "Pinnacle: 1.85 / 2.10" ou "1XBet home: 1.95"
            pin_home = re.search(r'pinnacle.*?(\d+\.\d+).*?(?:home|1|first)', text, re.I)
            pin_away = re.search(r'pinnacle.*?(\d+\.\d+).*?(?:away|2|second)', text, re.I)
            xbet_home = re.search(r'1xbet|onexbet.*?(\d+\.\d+).*?(?:home|1|first)', text, re.I)
            xbet_away = re.search(r'1xbet|onexbet.*?(\d+\.\d+).*?(?:away|2|second)', text, re.I)
            
            if pin_home:
                result.pinnacle_home = float(pin_home.group(1))
            if pin_away:
                result.pinnacle_away = float(pin_away.group(1))
            if xbet_home:
                result.onexbet_home = float(xbet_home.group(1))
            if xbet_away:
                result.onexbet_away = float(xbet_away.group(1))
            
            if any([result.pinnacle_home, result.onexbet_home]):
                result.confidence = 0.4  # Regex = moins confiant
                logger.info(f"⚠️ Gemini regex fallback: Pinnacle {result.pinnacle_home}/{result.pinnacle_away}")
                return result

            logger.warning("Gemini: No odds found in response")
            return None

        except Exception as e:
            logger.error(f"Parsing error: {e}")
            return None

    def _extract_odd(self, data: dict, path: list[str]) -> Optional[float]:
        """Extrait une cote imbriquée dans le dict."""
        try:
            current = data
            for key in path:
                current = current.get(key, {})
                if isinstance(current, (int, float)):
                    return float(current)
            return float(current) if current else None
        except:
            return None


# ═══════════════════════════════════════════════════════════════════
# INTEGRATION GROQ — Parsing ultra-rapide des réponses Gemini
# ═══════════════════════════════════════════════════════════════════

class GroqParser:
    """Parse les réponses texte de Gemini en JSON structuré via Groq."""

    def __init__(self):
        self.api_key = settings.groq_api_key
        self.model = settings.groq_model
        self.base_url = "https://api.groq.com/openai/v1/chat/completions"

    def parse_odds_response(self, gemini_text: str) -> Optional[dict]:
        """
        Transforme la prose de Gemini en JSON structuré.
        
        Args:
            gemini_text: Réponse textuelle de Gemini
            
        Returns:
            Dict structuré avec cotes ou None
        """
        if not self.api_key:
            logger.warning("Groq API key not configured")
            return None

        try:
            import requests

            system_prompt = """You are a JSON extraction specialist. 
Extract betting odds from the provided text and return ONLY a valid JSON object.

Required format:
{
    "pinnacle": {"home": float|null, "away": float|null, "draw": float|null},
    "onexbet": {"home": float|null, "away": float|null, "draw": float|null},
    "match_time": "ISO string or null",
    "confidence": "high|medium|low"
}

Rules:
- If a value is missing, use null
- Do not include any text outside the JSON
- Ensure valid JSON syntax"""

            response = requests.post(
                self.base_url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": f"Extract odds from this text:\n\n{gemini_text}"}
                    ],
                    "temperature": 0.1,
                    "max_tokens": 512,
                    "response_format": {"type": "json_object"}
                },
                timeout=10
            )

            if response.status_code != 200:
                logger.error(f"Groq error: {response.status_code}")
                return None

            result = response.json()
            content = result["choices"][0]["message"]["content"]
            return json.loads(content)

        except Exception as e:
            logger.error(f"Groq parsing error: {e}")
            return None


# ═══════════════════════════════════════════════════════════════════
# FACTORY — Point d'entrée unique
# ═══════════════════════════════════════════════════════════════════

def search_odds_with_fallback(
    home_team: str,
    away_team: str,
    sport: str = "soccer"
) -> Optional[GeminiOddsResult]:
    """
    Point d'entrée principal: recherche cotes via Gemini + Groq parsing.
    
    Args:
        home_team: Équipe à domicile
        away_team: Équipe extérieur  
        sport: Type de sport
        
    Returns:
        GeminiOddsResult avec cotes ou None
    """
    oracle = GeminiOracle()
    
    # Étape 1: Gemini Search
    result = oracle.search_odds(home_team, away_team, sport)
    
    if result and result.confidence >= 0.5:
        logger.info(f"✅ Gemini Oracle: High confidence odds for {home_team} vs {away_team}")
        return result
    
    logger.warning(f"⚠️ Gemini Oracle: Low confidence or no odds for {home_team} vs {away_team}")
    return result  # Retourne même si faible confiance, à la discrétion de l'appelant


# Singleton pour réutilisation
_oracle_instance: Optional[GeminiOracle] = None

def get_oracle() -> GeminiOracle:
    """Retourne l'instance singleton de l'Oracle."""
    global _oracle_instance
    if _oracle_instance is None:
        _oracle_instance = GeminiOracle()
    return _oracle_instance
