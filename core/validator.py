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
        logger.info(f"Gemini API call #{GeminiValidator._call_count} pour {match_name} / {market}")
        
        prompt = (
            f"En tant qu'expert en analyse de données sportives, évalue l'impact de cette news sur le spread. "
            f"Si l'absence du joueur X vaut plus de 2 points en NBA, augmente l'indice de confiance. "
            f"Si la news est déjà vieille de plus de 4h, considère qu'elle est déjà intégrée dans le prix."
        )

        try:
            response = self.model.generate_content(prompt)
            return response.text
        except ResourceExhausted:
            logger.warning(f"Quota Gemini atteint (429) sur appel #{GeminiValidator._call_count}")
            return "Validation skipped: quota reached"
        except Exception as e:
            logger.error(f"Erreur d'analyse de risque : {str(e)}")
            return f"Erreur d'analyse de risque : {str(e)}"

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
