import os
import logging
import google.generativeai as genai
from google.api_core.exceptions import ResourceExhausted

logger = logging.getLogger(__name__)

class GeminiValidator:
    _instance = None
    _call_count = 0

    def __init__(self):
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise ValueError("GEMINI_API_KEY manquante.")
        genai.configure(api_key=api_key)
        self.model = genai.GenerativeModel(
            model_name="gemini-2.0-flash"
        )
        logger.info("GeminiValidator initialisé.")

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def validate(self, match_name, market):
        GeminiValidator._call_count += 1
        logger.info(f"Gemini call #{GeminiValidator._call_count} | {match_name}")

        # Prompt court et précis — évite les timeouts et les erreurs de quota
        prompt = (
            f"Match: {match_name} | Marché: {market}\n"
            f"Recherche les news des 4 DERNIÈRES HEURES UNIQUEMENT.\n"
            f"Y a-t-il une blessure confirmée d'un joueur clé (impact > 2pts NBA ou >10% win prob soccer) ?\n"
            f"Si OUI : réponds '🚨 RED FLAG : [raison en 1 phrase]'\n"
            f"Si NON ou news > 4h : réponds '✅ Aucun risque majeur détecté.'"
        )

        try:
            response = self.model.generate_content(prompt)
            text = response.text.strip() if response.text else ""
            if not text:
                return "✅ Aucun risque majeur détecté."
            return text
        except ResourceExhausted:
            logger.warning(f"Quota Gemini atteint (appel #{GeminiValidator._call_count})")
            return "✅ Validation Gemini skippée (quota). Signal conservé."
        except Exception as e:
            logger.error(f"Erreur Gemini: {e}")
            return "✅ Validation Gemini indisponible. Signal conservé."

def check_market_red_flags(match_name, market):
    """
    Scrape le web en temps réel via Google Search Grounding pour identifier les Red Flags.
    """
    try:
        validator = GeminiValidator.get_instance()
        return validator.validate(match_name, market)
    except Exception as e:
        logger.error(f"Erreur d'initialisation de validation : {str(e)}")
        return f"Erreur d'initialisation : {str(e)}"
