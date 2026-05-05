import os
import requests
from flask import Flask, jsonify, render_template_string
from core.math_engine import calculate_shin_probabilities
from core.validator import check_market_red_flags
from core.context import get_market_news

app = Flask(__name__)

ODDS_API_KEY = os.environ.get("ODDS_API_KEY")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

@app.route('/api/screener', methods=['GET'])
def morning_screener():
    if not ODDS_API_KEY:
        return jsonify({"error": "Missing API Key"}), 500

    # 1. Fetch des marchés à 24h d'avance (upcoming)
    url = f"https://api.the-odds-api.com/v4/sports/upcoming/odds/?apiKey={ODDS_API_KEY}&regions=eu,us&markets=h2h,spreads&oddsFormat=decimal"
    response = requests.get(url)
    
    if response.status_code != 200:
        return jsonify({"error": "Failed to fetch odds"}), response.status_code
        
    matches = response.json()
    brief_items = []
    
    for match in matches:
        # Critère 3 : Filtre de Liquidité (Exclusion des divisions inférieures / sports illiquides)
        # On cible les ligues majeures définies par l'API
        if match['sport_key'] not in ['basketball_nba', 'soccer_epl', 'soccer_uefa_champs_league', 'tennis_atp', 'baseball_mlb']:
            continue
            
        home_team = match['home_team']
        away_team = match['away_team']
        match_name = f"{home_team} vs {away_team}"
        
        # Extraction des cotes Pinnacle (Sharp) et 1XBet (Soft)
        pinnacle_book = next((b for b in match['bookmakers'] if b['key'] == 'pinnacle'), None)
        one_xbet_book = next((b for b in match['bookmakers'] if b['key'] == 'onexbet'), None)
        
        if not pinnacle_book or not one_xbet_book:
            continue
            
        # Analyse du marché binaire H2H
        pinnacle_h2h = next((m for m in pinnacle_book['markets'] if m['key'] == 'h2h'), None)
        one_xbet_h2h = next((m for m in one_xbet_book['markets'] if m['key'] == 'h2h'), None)
        
        if not pinnacle_h2h or not one_xbet_h2h:
            continue
            
        try:
            pinnacle_odds = [outcome['price'] for outcome in pinnacle_h2h['outcomes']]
            one_xbet_odds = [outcome['price'] for outcome in one_xbet_h2h['outcomes']]
            
            if len(pinnacle_odds) != 2 or len(one_xbet_odds) != 2:
                continue # On force la synthèse binaire (pas de 1N2)
                
            # Calcul du Prix Intrinsèque (Shin)
            fair_probs = calculate_shin_probabilities(pinnacle_odds)
            fair_prices = [1.0 / p for p in fair_probs]
            
            for i, outcome in enumerate(one_xbet_h2h['outcomes']):
                cote_1xbet = outcome['price']
                fair_price_pinnacle = fair_prices[i]
                
                # Critère 1 : Le Spread d'Arbitrage Latent (Minimum 2.5%)
                alpha_spread = (cote_1xbet - fair_price_pinnacle) / fair_price_pinnacle
                
                status_clv = "✅ Confirmé" if alpha_spread > 0.025 else "❌ Rejeté (Spread trop faible)"
                
                # Lancement du Risk Flag IA uniquement si le critère mathématique est validé
                note_ia = "Non analysé (Spread insuffisant)"
                if alpha_spread > 0.025:
                    news = get_market_news(match_name, match['sport_title'])
                    note_ia = check_market_red_flags(match_name, f"Moneyline {outcome['name']}. Contexte : {news}")
                
                brief_items.append({
                    "sport": match['sport_title'],
                    "match": match_name,
                    "market": f"Moneyline {outcome['name']}",
                    "fair_price": round(fair_price_pinnacle, 2),
                    "price_1xbet": round(cote_1xbet, 2),
                    "spread": f"+{round(alpha_spread * 100, 1)}%" if alpha_spread > 0 else f"{round(alpha_spread * 100, 1)}%",
                    "clv": status_clv,
                    "note_ia": note_ia,
                    "valid": alpha_spread > 0.025
                })
        except Exception:
            continue

    # 2. Formatage du \"Morning Brief\" Telegram
    send_telegram_brief(brief_items)
    return jsonify({"status": "success", "processed_items": len(brief_items)})

def send_telegram_brief(items):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
        
    valid_items = [i for i in items if i['valid']]
    rejected_items = [i for i in items if not i['valid']][:2] # On en garde 2 pour l'exemple visuel
    
    message = f"📊 *PAIM SCREENER — 24h ADVANCE*\n"
    message += f"🟢 Status : {len(valid_items)} Opportunités détectées | ⏱ Scan : 06:00 GMT\n\n"
    
    idx = 1
    for item in valid_items:
        message += f"[{idx}] 🏀 *{item['sport']} - {item['match']}*\n"
        message += f"✔ Market : {item['market']}\n"
        message += f"✔ Fair Price (Pinnacle) : {item['fair_price']}\n"
        message += f"✔ 1XBet Price : {item['price_1xbet']}\n"
        message += f"⚡ Alpha Spread : {item['spread']} | CLV : {item['clv']}\n"
        message += f"⚠ Note IA : {item['note_ia']}\n\n"
        idx += 1
        
    if rejected_items:
        message += "━━━━━━━━━━━━━━━━━━━━\n"
        message += "📉 *EXEMPLES DE REJETS DYNAMIQUES :*\n\n"
        for item in rejected_items:
            message += f"❌ *{item['match']}* ({item['market']})\n"
            message += f"⚡ Alpha Spread : {item['spread']} | CLV : {item['clv']}\n\n"

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"})

@app.route('/', methods=['GET'])
def index():
    return "PAIM Morning Screener is active."
