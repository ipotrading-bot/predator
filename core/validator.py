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
        
        # Prompt PhD MIT — Impact quantitatif + Alpha Decay
        prompt = (
            f"En tant qu'expert en analyse de données sportives et pricing de marchés, "
            f"évalue l'événement : {match_name} pour le marché {market}.\n\n"
            f"Recherche sur le web les actualités des 4 dernières heures UNIQUEMENT.\n\n"
            f"CRITÈRES D'ÉVALUATION :\n"
            f"1. BLESSURE : Si un joueur clé est absent, estime l'impact en points de spread (NBA/NFL) "
            f"ou en probabilité de victoire (Soccer/Tennis). Une absence > 2 points d'impact = RED FLAG.\n"
            f"2. TIMING : Si la news a plus de 4 heures, elle est DÉJÀ INTÉGRÉE dans le prix. Ne la signale PAS.\n"
            f"3. MÉTÉO : Conditions extrêmes (pluie forte, vent >40km/h) uniquement pour sports outdoor.\n"
            f"4. LINEUP : Changement d'entraîneur ou rotation majeure confirmée dans les 2 dernières heures.\n\n"
            f"RÉPONSE ATTENDUE :\n"
            f"- Si impact quantifiable > 2 points OU news < 4h avec impact majeur : "
            f"'🚨 RED FLAG : [raison courte + impact estimé]'\n"
            f"- Sinon : '✅ Aucun risque majeur détecté. News déjà intégrée dans le pricing.'\n\n"
            f"Sois CONSERVATEUR. Un faux positif coûte moins cher qu'un faux négatif."
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
