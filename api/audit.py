import os
import requests
import logging
from datetime import datetime, timezone
from core.database import _get_supabase

logger = logging.getLogger(__name__)
ODDS_API_KEY = os.environ.get("ODDS_API_KEY")


def run_settlement_audit():
    """
    Récupère les matchs terminés, vérifie les scores et met à jour l'Alpha final.
    """
    sb = _get_supabase()
    if not sb:
        logger.warning("Supabase non configuré — audit impossible")
        return {"status": "error", "message": "Supabase non configuré"}

    # 1. Extraire les signaux en attente dont l'heure est passée
    now = datetime.now(timezone.utc).isoformat()
    res = sb.table("signals").select("*").eq("status", "pending").lt("match_time", now).execute()
    pending_signals = res.data

    if not pending_signals:
        print("✅ Aucun signal en attente d'audit.")
        return {"status": "success", "message": "Aucun signal à régler."}

    results = []
    for signal in pending_signals:
        # 2. Fetch du score final et de la closing line via The-Odds-API
        sport = signal['sport']
        score_url = f"https://api.the-odds-api.com/v4/sports/{sport}/scores/?apiKey={ODDS_API_KEY}&daysFrom=1"
        
        try:
            response = requests.get(score_url).json()
            # Trouver le match spécifique dans la réponse
            match_result = next((m for m in response if m['home_team'] + " vs " + m['away_team'] == signal['match_name']), None)
            
            if match_result and match_result['completed']:
                # 3. Logique de calcul du résultat (Binary Synthesis)
                outcome = determine_outcome(signal, match_result)
                
                # 4. Récupération de la Closing Line de Pinnacle
                closing_odds = fetch_pinnacle_closing(match_result) 
                
                # 5. Mise à jour Supabase via core/database
                from core.database import update_signal_settlement
                update_signal_settlement(signal['id'], outcome, closing_odds)
                
                results.append(signal['id'])
                print(f"📊 Signal {signal['match_name']} audité : {'GAGNÉ' if outcome == 1 else 'PERDU'}")
                
        except Exception as e:
            print(f"❌ Erreur audit sur {signal['match_name']} : {e}")
    
    return {"status": "success", "processed_signals": results}

def determine_outcome(signal, result):
    """
    Logique binaire pour AH 0.0 et Moneyline.
    """
    home_score = int(result['scores'][0]['score'])
    away_score = int(result['scores'][1]['score'])
    selection = signal['selection'] # ex: 'Lakers' ou 'Real Madrid'

    if home_score == away_score:
        return None # Void/Remboursé
    
    # Simple check for winner
    winner = result['home_team'] if home_score > away_score else result['away_team']
    return 1 if selection in winner else 0

def fetch_pinnacle_closing(match_result):
    # Logique pour extraire la dernière cote Pinnacle
    return 1.85 # Placeholder
