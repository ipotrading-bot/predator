from datetime import datetime, timedelta
import os
import requests
from flask import Flask, jsonify, render_template_string
from api.audit import run_settlement_audit
from core.math_engine import calculate_shin_probabilities
from core.validator import check_market_red_flags
from core.context import get_market_news
from supabase import create_client
import google.generativeai as genai

app = Flask(__name__)

ODDS_API_KEY = os.environ.get("ODDS_API_KEY")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
supabase = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None

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
            <!-- Dynamisé par JS -->
        </div>
    </div>

    <script>
        async function fetchAlphaSignals() {
            const grid = document.getElementById('screener-grid');
            try {
                const response = await fetch('/api/data');
                const signals = await response.json();
                if (signals.length === 0) {
                    grid.innerHTML = '<div class="p-10 text-center text-gray-600 uppercase tracking-widest">Aucun Alpha Spread détecté. Scan en cours...</div>';
                    return;
                }
                grid.innerHTML = signals.map(signal => {
                    const isPositive = signal.alpha_spread > 0.025;
                    const spreadColor = isPositive ? 'text-blue-400' : 'text-red-400';
                    return `
                    <div class="glass p-6 rounded-sm card-alpha flex flex-col md:flex-row justify-between items-center gap-6 animate-fade-in">
                        <div class="flex-1">
                            <h3 class="text-xl font-bold text-white">${signal.match_name}</h3>
                            <p class="text-sm text-green-400">Market: ${signal.market_type}</p>
                        </div>
                        <div class="flex gap-10 text-center">
                            <div><div class="text-[10px] text-gray-500 uppercase">Alpha Spread</div>
                            <div class="text-lg font-bold ${spreadColor}">+${(signal.alpha_spread * 100).toFixed(1)}%</div></div>
                        </div>
                        <div class="w-full md:w-64 p-3 bg-black/40 border border-green-900/30 rounded-sm">
                            <p class="text-[11px] text-gray-300 italic">${signal.note_ia || 'Analyse en cours...'}</p>
                        </div>
                    </div>`;
                }).join('');
            } catch (error) {
                grid.innerHTML = '<div class="p-10 text-center text-red-500">Erreur de connexion au flux.</div>';
            }
        }
        fetchAlphaSignals();
        setInterval(fetchAlphaSignals, 300000);
    </script>
</body>
</html>
"""

@app.route('/', methods=['GET'])
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route('/api/audit', methods=['POST'])
def audit_settlement():
    result = run_settlement_audit()
    return jsonify(result)

@app.route('/api/audit/weekly', methods=['GET'])
def weekly_performance_audit():
    if not supabase: return jsonify({"error": "Supabase non configuré"}), 500
    
    seven_days_ago = (datetime.now() - timedelta(days=7)).isoformat()
    res = supabase.table('signals').select("*").gt('created_at', seven_days_ago).execute()
    data = res.data

    if not data:
        return jsonify({"status": "error", "message": "Données insuffisantes pour l'audit."})

    total_trades = len(data)
    avg_clv = sum(s['alpha_spread'] for s in data) / total_trades
    wins = len([s for s in data if s.get('result') == 1])
    win_rate = (wins / total_trades) * 100

    analysis_prompt = f"""
    En tant qu'expert MIT en finance quantitative, analyse ce bilan hebdomadaire :
    - Nombre de signaux : {total_trades}
    - CLV Moyenne (Alpha capturé) : {avg_clv:.2%}
    - Win Rate Réalisé : {win_rate:.1f}%
    - Détail des sports : {[s['sport'] for s in data]}
    
    Identifie les biais : Quel sport performe le mieux ? La CLV est-elle en train de s'éroder ? 
    Donne 3 recommandations strictes pour la semaine prochaine.
    """
    
    model = genai.GenerativeModel('gemini-2.0-flash')
    report_ai = model.generate_content(analysis_prompt).text

    send_telegram_report(total_trades, avg_clv, win_rate, report_ai)
    
    return jsonify({"status": "success", "audit": "Rapport envoyé"})

def send_telegram_report(total_trades, avg_clv, win_rate, report_ai):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    
    message = f"📊 *PAIM STRATEGIC AUDIT*\n"
    message += f"• *Volume:* {total_trades} Signaux\n"
    message += f"• *Alpha Moyen:* {avg_clv:.2%}\n"
    message += f"• *Win Rate:* {win_rate:.1f}%\n\n"
    message += f"🧠 *ANALYSE IA:*\n{report_ai}"
    
    requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", 
                  json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"})

@app.route('/api/research', methods=['GET'])
def get_research_data():
    # Placeholder for research data (e.g., aggregated analytics)
    return jsonify({"status": "success", "message": "Research data placeholder"})

@app.route('/api/portfolio', methods=['GET'])
def get_portfolio_data():
    # Placeholder for portfolio data (e.g., exposure, risk)
    return jsonify({"status": "success", "message": "Portfolio data placeholder"})

@app.route('/api/sentiment', methods=['GET'])
def get_sentiment_data():
    # Placeholder for sentiment data (e.g., market sentiment)
    return jsonify({"status": "success", "message": "Sentiment data placeholder"})

@app.route('/api/data', methods=['GET'])
def get_screener_data():
    if not supabase: return jsonify({"error": "Supabase non configuré"}), 500
    try:
        response = supabase.table('signals').select("*").order('created_at', desc=True).limit(10).execute()
        # LOGGING pour déboguer le schema mismatch
        if response.data:
            print(f"DEBUG: Schema des données (1er record): {list(response.data[0].keys())}")
        return jsonify(response.data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/research', methods=['GET'])
def get_research():
    return jsonify({"status": "ok", "message": "Research data placeholder"})

@app.route('/api/portfolio', methods=['GET'])
def get_portfolio():
    return jsonify({"status": "ok", "message": "Portfolio data placeholder"})

@app.route('/api/sentiment', methods=['GET'])
def get_sentiment():
    return jsonify({"status": "ok", "message": "Sentiment data placeholder"})

@app.route('/api/test-seed', methods=['POST'])
def test_seed():
    if not supabase: return jsonify({"error": "Supabase non configuré"}), 500
    test_signal = {
        "match_name": "Real Madrid vs Bayern Munich",
        "match_time": "2026-05-08T19:00:00Z",
        "sport": "Soccer",
        "market_type": "Moneyline",
        "fair_price": 1.85,
        "cote_1xbet": 2.05,
        "alpha_spread": 0.108,
        "is_elite_signal": True,
        "status": "pending",
        "note_ia": "✅ Signal fictif pour test Dashboard."
    }
    response = supabase.table('signals').insert(test_signal).execute()
    return jsonify(response.data)

@app.route('/api/screener', methods=['GET'])
def morning_screener():
    if not ODDS_API_KEY: return jsonify({"error": "Missing API Key"}), 500

    url = f"https://api.the-odds-api.com/v4/sports/upcoming/odds/?apiKey={ODDS_API_KEY}&regions=eu,us&markets=h2h&oddsFormat=decimal"
    response = requests.get(url)
    if response.status_code != 200: return jsonify({"error": "Failed to fetch odds"}), response.status_code
        
    matches = response.json()
    print(f'Matchs reçus de l API: {len(matches)}')
    brief_items = []
    
    for match in matches:
        # Filter supprimé pour test
        match_name = f"{match['home_team']} vs {match['away_team']}"
        pinnacle_book = next((b for b in match['bookmakers'] if b['key'] == 'pinnacle'), None)
        one_xbet_book = next((b for b in match['bookmakers'] if b['key'] == 'onexbet'), None)
        if not pinnacle_book or not one_xbet_book: continue
        pinnacle_h2h = next((m for m in pinnacle_book['markets'] if m['key'] == 'h2h'), None)
        one_xbet_h2h = next((m for m in one_xbet_book['markets'] if m['key'] == 'h2h'), None)
        if not pinnacle_h2h or not one_xbet_h2h: continue
            
        try:
            pinnacle_odds = [outcome['price'] for outcome in pinnacle_h2h['outcomes']]
            fair_prices = [1.0/p for p in calculate_shin_probabilities(pinnacle_odds)]
            
            for i, outcome in enumerate(one_xbet_h2h['outcomes']):
                cote_1xbet = outcome['price']
                alpha_spread = (cote_1xbet - fair_prices[i]) / fair_prices[i]
                
                if alpha_spread > 0.001:
                    news = get_market_news(match_name, match['sport_title'])
                    note_ia = check_market_red_flags(match_name, f"Moneyline {outcome['name']}. Contexte : {news}")
                    
                    signal_data = {
                        "match_name": match_name,
                        "sport": match['sport_title'],
                        "market_type": f"Moneyline {outcome['name']}",
                        "fair_price": round(fair_prices[i], 2),
                        "cote_1xbet": round(cote_1xbet, 2),
                        "alpha_spread": round(alpha_spread, 4),
                        "note_ia": note_ia
                    }
                    if supabase: supabase.table('signals').insert(signal_data).execute()
                    brief_items.append(signal_data)
        except Exception: continue

    return jsonify({"status": "success", "items": brief_items})
