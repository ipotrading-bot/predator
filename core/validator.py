import os
import google.generativeai as genai

def check_market_red_flags(match_name, market):
    """
    Scrape le web en temps réel via Google Search Grounding pour identifier les Red Flags.
    """
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return "Clé API Gemini manquante."
        
    genai.configure(api_key=api_key)
    
    # Configuration du modèle avec l'outil de recherche Google activé
    model = genai.GenerativeModel(
        model_name="gemini-2.0-flash",
        tools=[{"google_search": {}}]
    )
    
    prompt = (
        f"Analyse l'événement sportif à venir : {match_name} pour le marché {market}. "
        f"Recherche sur le web les actualités des 12 dernières heures. "
        f"Y a-t-il une blessure de dernière minute d'un joueur clé, un changement d'entraîneur subit, "
        f"ou des conditions météo extrêmes ? "
        f"Si un risque majeur est détecté, résume-le en une phrase courte commençant par '🚨 RED FLAG : '. "
        f"Si aucun risque n'est détecté, réponds strictement par '✅ Aucun risque majeur détecté.'"
    )
    
    try:
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        return f"Erreur d'analyse de risque : {str(e)}"
