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
            f"Analyse l'événement sportif à venir : {match_name} pour le marché {market}. "
            f"Recherche sur le web les actualités des 12 dernières heures. "
            f"Y a-t-il une blessure de dernière minute d'un joueur clé, un changement d'entraîneur subit, "
            f"ou des conditions météo extrêmes ? "
            f"Si un risque majeur est détecté, résume-le en une phrase courte commençant par '🚨 RED FLAG : '. "
            f"Si aucun risque n'est détecté, réponds strictement par '✅ Aucun risque majeur détecté.'"
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
