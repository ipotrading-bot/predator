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

# Style CSS "Quant-Elite"
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="fr">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>PAIM | Morning Screener Terminal</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&display=swap" rel="stylesheet">
    <style>
        body { font-family: 'JetBrains Mono', monospace; background-color: #050505; color: #00ff00; }
        .glass { background: rgba(255, 255, 255, 0.02); border: 1px solid rgba(0, 255, 0, 0.1); backdrop-filter: blur(10px); }
        .card-alpha { border-left: 4px solid #00ff00; transition: all 0.3s ease; }
        .card-alpha:hover { background: rgba(0, 255, 0, 0.05); transform: translateX(5px); }
        .status-pulse { animation: pulse 2s infinite; }
        @keyframes pulse { 0% { opacity: 1; } 50% { opacity: 0.4; } 100% { opacity: 1; } }
    </style>
</head>
<body class="p-4 md:p-8">
    <!-- Header -->
    <div class="max-w-6xl mx-auto flex justify-between items-end mb-8 border-b border-green-900 pb-6">
        <div>
            <h1 class="text-3xl font-bold tracking-tighter text-white">🦅 PREDATOR <span class="text-green-500">PAIM</span></h1>
            <p class="text-xs text-green-700 mt-1 uppercase tracking-widest">PhD MIT Quant System | Dakar Hub</p>
        </div>
        <div class="text-right">
            <div class="text-xs text-gray-500 mb-1">SYSTEM STATUS</div>
            <div class="flex items-center justify-end gap-2">
                <span class="h-2 w-2 bg-green-500 rounded-full status-pulse"></span>
                <span class="text-sm font-bold text-green-400">ACTIVE SCREENER</span>
            </div>
        </div>
    </div>

    <!-- Stats Bar -->
    <div class="max-w-6xl mx-auto grid grid-cols-1 md:grid-cols-4 gap-4 mb-10">
        <div class="glass p-4 rounded-sm">
            <div class="text-[10px] text-gray-500 uppercase">Target ROI</div>
            <div class="text-xl font-bold text-white">+100% / MOIS</div>
        </div>
        <div class="glass p-4 rounded-sm">
            <div class="text-[10px] text-gray-500 uppercase">Alpha Threshold</div>
            <div class="text-xl font-bold text-green-500">> 2.5%</div>
        </div>
        <div class="glass p-4 rounded-sm">
            <div class="text-[10px] text-gray-500 uppercase">Screener Cycle</div>
            <div class="text-xl font-bold text-white">06:00 GMT</div>
        </div>
        <div class="glass p-4 rounded-sm">
            <div class="text-[10px] text-gray-500 uppercase">Risk Management</div>
            <div class="text-xl font-bold text-blue-400 text-sm">FRACTIONAL KELLY</div>
        </div>
    </div>

    <!-- Main Content -->
    <div class="max-w-6xl mx-auto">
        <div class="flex justify-between items-center mb-6">
            <h2 class="text-lg font-bold text-white uppercase tracking-tight">Opportunités Détectées (J+1)</h2>
            <button onclick="location.href='/api/screener'" class="text-[10px] bg-green-600 hover:bg-green-500 text-black px-4 py-2 font-bold rounded-sm transition-colors">
                FORCE MANUAL SCAN
            </button>
        </div>

        <!-- Opportunity Grid -->
        <div id="screener-grid" class="grid grid-cols-1 gap-4">
            <!-- Contenu dynamique -->
        </div>
    </div>

    <footer class="max-w-6xl mx-auto mt-20 pt-6 border-t border-gray-900 text-center">
        <p class="text-[10px] text-gray-600 uppercase tracking-widest">
            Propriété Intellectuelle PAIM System | Algorithmic Information Arbitrage
        </p>
    </footer>
</body>
</html>
"""

@app.route('/', methods=['GET'])
def index():
    return render_template_string(HTML_TEMPLATE)

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
        # Critère 3 : Filtre de Liquidité
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
                continue
                
            # Calcul du Prix Intrinsèque (Shin)
            fair_probs = calculate_shin_probabilities(pinnacle_odds)
            fair_prices = [1.0 / p for p in fair_probs]
            
            for i, outcome in enumerate(one_xbet_h2h['outcomes']):
                cote_1xbet = outcome['price']
                fair_price_pinnacle = fair_prices[i]
                
                # Critère 1 : Le Spread d'Arbitrage Latent (Minimum 2.5%)
                alpha_spread = (cote_1xbet - fair_price_pinnacle) / fair_price_pinnacle
                
                status_clv = "✅ Confirmé" if alpha_spread > 0.025 else "❌ Rejeté"
                
                # Lancement du Risk Flag IA
                note_ia = "Non analysé"
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

    # 2. Formatage du "Morning Brief" Telegram
    send_telegram_brief(brief_items)
    return jsonify({"status": "success", "items": brief_items})

def send_telegram_brief(items):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
        
    valid_items = [i for i in items if i['valid']]
    
    if not valid_items:
        return
    
    message = f"📊 *PAIM SCREENER — 24h ADVANCE*\n"
    message += f"🟢 Status : {len(valid_items)} Opportunités détectées\n\n"
    
    for idx, item in enumerate(valid_items[:5], 1):
        message += f"[{idx}] 🏀 *{item['sport']} - {item['match']}*\n"
        message += f"✔ {item['market']} | Spread : {item['spread']}\n"
        message += f"⚠ IA : {item['note_ia'][:50]}...\n\n"

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"})
