from datetime import datetime, timedelta
import os
import asyncio
from flask import Flask, jsonify, render_template, request
from api.audit import run_settlement_audit
from api.logger import create_scan_logger
from core.math_engine import calculate_shin_probabilities
from core.validator import check_market_red_flags
from core.context import get_market_news
from data.supabase_client import SupabaseClient
from supabase import create_client
import google.generativeai as genai
from config import settings
from signals.scanner import MarketScanner
from core.paim_engine import PAIMEngine
from core.notifications import TelegramNotifier

app = Flask(__name__)

genai.configure(api_key=settings.gemini_api_key)

db = SupabaseClient()

ODDS_API_KEY = os.environ.get("ODDS_API_KEY")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
supabase = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None


def _fetch_context_fallback(home: str, away: str) -> str:
    """Fallback quand NEWS_API_KEY manque - utilise Reddit + FlashScore gratuit"""
    import requests
    context = []

    # Option A: Reddit (gratuit, sans clé)
    try:
        reddit_url = f"https://www.reddit.com/r/soccer/search.json?q={home}+{away}&limit=3"
        reddit_data = requests.get(reddit_url, headers={"User-Agent": "PREDATOR/3.0"}).json()
        titles = [post["data"]["title"] for post in reddit_data.get("data", {}).get("children", [])]
        if titles:
            context.append(f"Reddit buzz: {' | '.join(titles[:2])}")
    except:
        pass

    # Option B: FlashScore direct (scraping minimal)
    try:
        fs_url = f"https://www.flashscore.com/match/{home.lower()}-{away.lower()}/#match-summary"
        context.append(f"Check FlashScore injuries: {fs_url}")
    except:
        pass

    return "\n".join(context) if context else "No external context available"


def _build_ai_context(event: dict) -> str:
    """Construit le contexte AI : pipeline premium (NewsAPI) ou fallback zéro clé"""
    if settings.news_api_key:
        # Pipeline premium
        news = get_market_news(f"{event['home_team']} {event['away_team']}", event.get('sport', ''))
        return f"Latest News: {news}"
    else:
        # Pipeline fallback (zéro clé)
        return _fetch_context_fallback(event["home_team"], event["away_team"])


def gemini_risk_check(event_name, market):
    return check_market_red_flags(event_name, market)

@app.route('/', methods=['GET'])
def index():
    return render_template('index.html')

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
    
    data = PAIMEngine.fetch_weekly_data(supabase)

    if not data:
        return jsonify({"status": "error", "message": "Données insuffisantes pour l'audit."})

    audit = PAIMEngine.run_weekly_audit(data)
    if not audit:
        return jsonify({"status": "error", "message": "Audit impossible."})

    report_ai = PAIMEngine.get_ai_analysis(
        audit['total_trades'], audit['avg_clv'], audit['win_rate'], [s['sport'] for s in data]
    )

    asyncio.run(TelegramNotifier().send_audit_report(
        audit['total_trades'], audit['avg_clv'], audit['win_rate'], report_ai
    ))
    
    return jsonify({"status": "success", "audit": "Rapport envoyé"})

@app.route('/api/screener', methods=['GET'])
def morning_screener():
    # Run scan asynchronously
    result = asyncio.run(MarketScanner().run_scan())
    return jsonify({"status": "success", "result": result.__dict__})
