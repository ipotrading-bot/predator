"""
core/gemini_search.py — Gemini 2.0 Flash avec Google Search Grounding
Fallback Oracle quand The-Odds-API est épuisée ou sans données.

Stratégie:
1. Search Grounding pour trouver cotes Pinnacle (sharp) et 1XBet (soft)
2. Parsing JSON structuré via Groq
3. Calcul Shin Method sur données récupérées

v7.0 GUERRILLA DATA — Support Harvester + Oracle + Broad Search
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
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
        self.model = "gemini-2.0-flash"  # v7.0: corrigé (était flash-exp)
        self.base_url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"

    def _build_match_odds_search_prompt(self, home_team: str, away_team: str, sport: str = "soccer") -> str:
        """
        Construit le prompt optimisé pour extraction de cotes.
        v7.0: Support multi-marchés (h2h + totals + btts)
        """
        return f"""You are a specialized odds extraction agent for arbitrage detection.

TASK: Find current betting odds for this {sport.upper()} match:
{home_team} vs {away_team}

SEARCH INSTRUCTIONS:
1. Use Google Search to find CURRENT odds from:
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
- Confidence: high/medium/low based on how recent the data is
- If odds not found, return null values"""

    def _build_harvester_decode_prompt(self, raw_json: str) -> str:
        """
        v7.0: Prompt de décodage du flux brut 1XBet.
        Transforme le JSON chaotique en structure PAIM.
        """
        return f"""Tu es un parseur de données JSON expert. Voici un extrait du flux brut de 1XBet :

{raw_json[:2000]}

Ta mission :
1. Identifie le nom des deux équipes.
2. Extrais la cote de la Victoire 1, du Nul, et de la Victoire 2.
3. Extrais la ligne du Handicap Asiatique 0.0 si elle existe.
4. Réponds UNIQUEMENT sous format JSON structuré :
{{"match": "Team A vs Team B", "odds": {{"1": 1.85, "X": 3.40, "2": 4.10}}, "ah0": 1.65}}

Si aucune donnée exploitable : {{"match": null}}"""

    def _build_pinnacle_oracle_prompt(self, match_name: str) -> str:
        """
        v7.0: Prompt pour obtenir le Fair Price Pinnacle via Google.
        """
        return f"""Cherche sur Google la cote actuelle de Pinnacle (Sharp) pour le match {match_name}
sur le marché binaire. Donne-moi juste le chiffre de la cote la plus basse (le favori).
Retourne UNIQUEMENT un JSON : {{"price": 1.85, "team": "Team Name", "confidence": "high"}}
Si introuvable : {{"price": null}}"""

    def search_odds(self, home_team: str, away_team: str, sport: str = "soccer") -> Optional[GeminiOddsResult]:
        """
        Recherche les cotes via Gemini Search Grounding.
        
        Returns:
            GeminiOddsResult avec cotes trouvées ou None si échec
        """
        try:
            import requests

            prompt = self._build_match_odds_search_prompt(home_team, away_team, sport)

            payload = {
                "contents": [{
                    "role": "user",
                    "parts": [{"text": prompt}]
                }],
                "tools": [{"google_search": {}}],
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

            return self._parse_odds_from_text(raw_text, home_team, away_team)

        except Exception as e:
            logger.error(f"Erreur Gemini Search: {e}")
            return None

    def get_pinnacle_oracle(self, match_name: str) -> Optional[float]:
        """
        v7.0: Interroge Google Search via Gemini pour obtenir le prix Pinnacle.
        
        Args:
            match_name: "Team A vs Team B"
            
        Returns:
            float: Cote Pinnacle du favori ou None
        """
        try:
            import requests
            
            prompt = self._build_pinnacle_oracle_prompt(match_name)
            payload = {
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "tools": [{"google_search": {}}],
                "generationConfig": {"temperature": 0.1, "maxOutputTokens": 256},
            }
            r = requests.post(f"{self.base_url}?key={self.api_key}", json=payload, timeout=20)
            if r.status_code != 200:
                return None
            
            result = r.json()
            candidates = result.get("candidates", [])
            if not candidates:
                return None
            
            text = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "")
            m = re.search(r'"price"\s*:\s*(\d+\.\d+)', text)
            if m:
                return float(m.group(1))
            # Fallback: chercher un nombre décimal dans le texte
            nums = re.findall(r'(\d+\.\d+)', text)
            if nums:
                return float(nums[0])
            return None
        except Exception as e:
            logger.error(f"Pinnacle Oracle error: {e}")
            return None

    def decode_harvester_feed(self, raw_json: str) -> Optional[dict]:
        """
        v7.0: Gemini decode le flux brut 1XBet en structure PAIM.
        
        Args:
            raw_json: Flux JSON brut du Harvester 1XBet
            
        Returns:
            dict: {"match": "...", "odds": {...}, "ah0": ...} ou None
        """
        try:
            import requests
            
            prompt = self._build_harvester_decode_prompt(raw_json)
            payload = {
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.1, "maxOutputTokens": 512},
            }
            r = requests.post(f"{self.base_url}?key={self.api_key}", json=payload, timeout=30)
            if r.status_code != 200:
                return None
            
            result = r.json()
            candidates = result.get("candidates", [])
            if not candidates:
                return None
            
            text = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "")
            return self._extract_json(text)
        except Exception as e:
            logger.error(f"Harvester decode error: {e}")
            return None

    def broad_search_upcoming_events(self) -> list[dict]:
        """
        v7.0: Recherche large via Gemini pour trouver les matchs importants.
        Supprimé le doublon — méthode unique, refactorée.
        """
        broad_prompt = (
            'Cherche les 10 matchs de Tennis ATP et de Football (Top 5) les plus importants des prochaines 24h. '
            'Pour chaque match, trouve la cote actuelle de Pinnacle et de 1XBet sur le marché binaire (ML ou AH 0.0). '
            'Retourne une liste de matchs au format JSON : '
            '[{"sport_title": "...", "commence_time": "ISO String", "home_team": "...", "away_team": "...", '
            '"bookmakers": [{"key": "pinnacle", "markets": [{"key": "h2h", "outcomes": [{"name": "Home", "price": 0.0}, {"name": "Away", "price": 0.0}]}]}, '
            '{"key": "1xbet", "markets": [{"key": "h2h", "outcomes": [{"name": "Home", "price": 0.0}, {"name": "Away", "price": 0.0}]}]}]}]'
        )

        payload = {
            "contents": [{"role": "user", "parts": [{"text": broad_prompt}]}],
            "tools": [{"google_search": {}}],
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": 2048},
        }

        all_events = []
        try:
            import requests
            response = requests.post(
                f"{self.base_url}?key={self.api_key}", json=payload, timeout=45
            )
            if response.status_code != 200:
                logger.error(f"Gemini broad search API error: {response.status_code}")
                return []

            result = response.json()
            candidates = result.get("candidates", [])
            if not candidates:
                return []

            raw_text = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "")

            # Direct JSON parse
            try:
                parsed = json.loads(raw_text)
                if isinstance(parsed, list):
                    for ev in parsed:
                        ev["source"] = "AI Search"
                        all_events.append(ev)
                    logger.info(f"✅ Gemini broad: {len(all_events)} events parsed directly")
                    return all_events
            except json.JSONDecodeError:
                pass

            # Regex fallback
            match_pattern = re.compile(r"(.*?)\s+vs\s+(.*?)\s+\((.*?)\)\s+-\s+(.*)")
            matches = match_pattern.findall(raw_text)
            for home, away, sport_str, time_str in matches:
                odds_result = self.search_odds(home.strip(), away.strip(), sport_str.strip())
                if odds_result:
                    bms = []
                    if odds_result.pinnacle_home and odds_result.pinnacle_away:
                        bms.append({"key": "pinnacle", "markets": [{"key": "h2h", "outcomes": [
                            {"name": home.strip(), "price": odds_result.pinnacle_home},
                            {"name": away.strip(), "price": odds_result.pinnacle_away}
                        ]}]})
                    if odds_result.onexbet_home and odds_result.onexbet_away:
                        bms.append({"key": "1xbet", "markets": [{"key": "h2h", "outcomes": [
                            {"name": home.strip(), "price": odds_result.onexbet_home},
                            {"name": away.strip(), "price": odds_result.onexbet_away}
                        ]}]})
                    if bms:
                        all_events.append({
                            "id": f"broad_{home}_{away}",
                            "sport_title": sport_str.strip(),
                            "commence_time": time_str,
                            "home_team": home.strip(),
                            "away_team": away.strip(),
                            "bookmakers": bms,
                            "source": "AI Search"
                        })
            return all_events

        except Exception as e:
            logger.error(f"Erreur Gemini broad search: {e}")
            return []

    def _extract_json(self, text: str) -> Optional[dict]:
        """Extrait le premier bloc JSON valide du texte."""
        m = re.search(r'\{[\s\S]*\}', text)
        if m:
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                pass
        return None

    def _parse_odds_from_text(self, text: str, home: str, away: str) -> Optional[GeminiOddsResult]:
        """
        Parse le JSON des cotes depuis la réponse texte de Gemini.
        Fallback regex si JSON malformé.
        """
        result = GeminiOddsResult(home_team=home, away_team=away)

        try:
            # Essayer d'extraire JSON structuré
            json_match = re.search(r'\{[\s\S]*"pinnacle"[\s\S]*\}', text, re.DOTALL)
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
            pin_home = re.search(r'pinnacle.*?(\d+\.\d+).*?(?:home|1|first)', text, re.I | re.DOTALL)
            pin_away = re.search(r'pinnacle.*?(\d+\.\d+).*?(?:away|2|second)', text, re.I | re.DOTALL)
            xbet_home = re.search(r'1xbet|onexbet.*?(\d+\.\d+).*?(?:home|1|first)', text, re.I | re.DOTALL)
            xbet_away = re.search(r'1xbet|onexbet.*?(\d+\.\d+).*?(?:away|2|second)', text, re.I | re.DOTALL)
            
            if pin_home:
                result.pinnacle_home = float(pin_home.group(1))
            if pin_away:
                result.pinnacle_away = float(pin_away.group(1))
            if xbet_home:
                result.onexbet_home = float(xbet_home.group(1))
            if xbet_away:
                result.onexbet_away = float(xbet_away.group(1))
            
            if any([result.pinnacle_home, result.onexbet_home]):
                result.confidence = 0.4
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
        except Exception as e:
            logger.error(f"Error extracting odd: {e}")
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


def search_upcoming_events_from_gemini() -> list[dict]:
    """
    Effectue une recherche large via Gemini.
    Fallback quand The-Odds-API est vide.
    """
    oracle = GeminiOracle()
    return oracle.broad_search_upcoming_events()


def get_pinnacle_fair_price(match_name: str) -> Optional[float]:
    """
    v7.0: Utilise Google Search de Gemini pour trouver la cote Pinnacle actuelle.
    
    Args:
        match_name: "Team A vs Team B"
        
    Returns:
        float: Cote Pinnacle du favori ou None
    """
    oracle = GeminiOracle()
    return oracle.get_pinnacle_oracle(match_name)


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
    return result


# Singleton pour réutilisation
_oracle_instance: Optional[GeminiOracle] = None

def get_oracle() -> GeminiOracle:
    """Retourne l'instance singleton de l'Oracle."""
    global _oracle_instance
    if _oracle_instance is None:
        _oracle_instance = GeminiOracle()
    return _oracle_instance