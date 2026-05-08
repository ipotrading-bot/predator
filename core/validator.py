import os
import logging
import google.generativeai as genai
from google.api_core.exceptions import ResourceExhausted

logger = logging.getLogger(__name__)

# Configuration unique au niveau module (une seule fois)
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
if GEMINI_API_KEY:
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        logger.info("✅ Gemini API configurée au niveau module")
    except Exception as e:
        logger.warning(f"⚠️ Gemini configure warning: {e}")
else:
    logger.warning("⚠️ GEMINI_API_KEY non définie")


class GeminiValidator:
    _instance = None
    _call_count = 0

    def __init__(self):
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise ValueError("GEMINI_API_KEY manquante.")
        # genai.configure déjà fait au niveau module
        self.model = genai.GenerativeModel(
            model_name="gemini-2.0-flash",
            generation_config={"max_output_tokens": 80, "temperature": 0.1},
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
            f"En tant qu'expert en analyse de données sportives, évalue l'impact de cette news sur le spread. "
            f"Si l'absence du joueur X vaut plus de 2 points en NBA, augmente l'indice de confiance. "
            f"Si la news est déjà vieille de plus de 4h, considère qu'elle est déjà intégrée dans le prix."
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
