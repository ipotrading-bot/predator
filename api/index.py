from datetime import datetime, timedelta
import os
import requests
from flask import Flask, jsonify, render_template_string, request
from api.audit import run_settlement_audit
from api.logger import create_scan_logger
from core.math_engine import calculate_shin_probabilities
from core.validator import check_market_red_flags
from core.context import get_market_news
from data.supabase_client import SupabaseClient
from supabase import create_client
import google.generativeai as genai
from config import settings

app = Flask(__name__)

genai.configure(api_key=settings.gemini_api_key)

db = SupabaseClient()

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

def gemini_risk_check(event_name, market):
    return check_market_red_flags(event_name, market)

def _send_morning_brief():
    if not supabase: return
    
    one_day_ago = (datetime.now() - timedelta(days=1)).isoformat()
    res = supabase.table('signals').select("*").gt('created_at', one_day_ago).execute()
    signals = res.data
    
    elite_signals = [s for s in signals if s.get('is_elite_signal')][:5]
    display_signals = [s for s in signals if 0.015 <= s.get('alpha_spread', 0) <= 0.025]
    
    message = "🌅 *MORNING BRIEF PREDATOR PAIM*\n\n"
    if not elite_signals and not display_signals:
        message += "Marché trop efficient — capital préservé ✅"
    else:
        if elite_signals:
            message += "🔥 *ELITE SIGNALS:*\n"
            for s in elite_signals:
                message += f"• {s['match_name']} — Alpha: {s.get('alpha_spread', 0):.2%}\n"
            message += "\n"
        if display_signals:
            message += "📈 *DISPLAY SIGNALS (1.5-2.5%):*\n"
            for s in display_signals:
                message += f"• {s['match_name']} — Alpha: {s.get('alpha_spread', 0):.2%}\n"
    
    requests.post(f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage", 
                  json={"chat_id": settings.telegram_chat_id, "text": message, "parse_mode": "Markdown"})

@app.route('/', methods=['GET'])
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route('/api/search', methods=['GET'])
def search_signals_api():
    date = request.args.get('date')
    time = request.args.get('time')
    sport = request.args.get('sport')
    results = db.search_signals(sport=sport, date=date, time=time)
    return jsonify(results)

@app.route('/api/info', methods=['GET'])
def get_info_pages_api():
    results = db.get_info_pages()
    return jsonify(results)

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
def get_research():
    return jsonify({"status": "ok", "message": "Research data placeholder"})

@app.route('/api/portfolio', methods=['GET'])
def get_portfolio():
    return jsonify({"status": "ok", "message": "Portfolio data placeholder"})

@app.route('/api/sentiment', methods=['GET'])
def get_sentiment():
    return jsonify({"status": "ok", "message": "Sentiment data placeholder"})

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
    logger = create_scan_logger()
    logger.start("Starting morning screener scan")
    
    if not ODDS_API_KEY:
        logger.error("Missing Odds API Key")
        return jsonify({"error": "Missing API Key"}), 500

    url = f"https://api.the-odds-api.com/v4/sports/upcoming/odds/?apiKey={ODDS_API_KEY}&regions=eu,us&markets=h2h&oddsFormat=decimal"
    logger.debug(f"Fetching odds from: {url}")
    
    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
    except Exception as e:
        logger.error("Failed to fetch odds", exc=e)
        return jsonify({"error": "Failed to fetch odds"}), 500
        
    matches = response.json()
    logger.info(f"Received {len(matches)} matches from API")
    brief_items = []
    
    for match in matches:
        match_name = f"{match['home_team']} vs {match['away_team']}"
        logger.debug(f"Processing match: {match_name}")
        
        pinnacle_book = next((b for b in match['bookmakers'] if b['key'] == 'pinnacle'), None)
        one_xbet_book = next((b for b in match['bookmakers'] if b['key'] == 'onexbet'), None)
        
        if not pinnacle_book or not one_xbet_book:
            logger.debug(f"Skipping match {match_name}: Missing bookmakers (Pinnacle/1xBet)")
            continue
            
        pinnacle_h2h = next((m for m in pinnacle_book['markets'] if m['key'] == 'h2h'), None)
        one_xbet_h2h = next((m for m in one_xbet_book['markets'] if m['key'] == 'h2h'), None)
        
        if not pinnacle_h2h or not one_xbet_h2h:
            logger.debug(f"Skipping match {match_name}: Missing H2H market")
            continue
            
        try:
            pinnacle_odds = [outcome['price'] for outcome in pinnacle_h2h['outcomes']]
            fair_prices = [1.0/p for p in calculate_shin_probabilities(pinnacle_odds)]
            
            for i, outcome in enumerate(one_xbet_h2h['outcomes']):
                cote_1xbet = outcome['price']
                
                # Validation check
                if i >= len(fair_prices):
                    logger.warning(f"Mismatch in outcomes for {match_name}")
                    continue
                    
                alpha_spread = (cote_1xbet - fair_prices[i]) / fair_prices[i]
                
                if alpha_spread > 0.001:
                    logger.info(f"Alpha spread detected for {match_name}: {alpha_spread:.2%}")
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
                    
                    if supabase:
                        try:
                            supabase.table('signals').insert(signal_data).execute()
                            logger.info(f"Signal inserted for {match_name}")
                        except Exception as e:
                            logger.error(f"Failed to insert signal for {match_name}", exc=e)
                    
                    brief_items.append(signal_data)
        except Exception as e:
            logger.error(f"Error processing {match_name}", exc=e)
            continue

    logger.complete(f"Scan finished. Processed {len(brief_items)} signals.")
    return jsonify({"status": "success", "items": brief_items})
