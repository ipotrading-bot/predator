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

# Configure Gemini AI lazily (only when needed, not at import)
try:
    genai.configure(api_key=settings.gemini_api_key)
except Exception:
    pass  # Will fail gracefully when used

db = SupabaseClient()

# Chercher 1XBet avec gestion des synonymes
BOOKMAKER_SYNONYMS = ['onexbet', '1xbet', '1x_bet', '1xbit']


def _find_1xbet(bookmakers: list[dict]) -> list[dict]:
    """Trouve 1XBet parmi les bookmakers, avec gestion des synonymes."""
    for bm in bookmakers:
        if bm.get("key", "").lower() in BOOKMAKER_SYNONYMS:
            return bm.get("markets", [])
    return []


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


# ───── Page principale ─────

@app.route('/', methods=['GET'])
def index():
    return render_template('index.html')


# ───── Endpoints Dashboard ─────

@app.route('/api/data', methods=['GET'])
def get_signals_data():
    """Signaux live pour le dashboard principal."""
    try:
        signals = db._client.table("signals").select("*").eq("status", "pending").order("created_at", desc=True).limit(50).execute()
        return jsonify(signals.data or [])
    except Exception as e:
        return jsonify([])


@app.route('/api/stats', methods=['GET'])
def get_stats():
    """Stats globales pour les cards du dashboard."""
    try:
        summary = db.get_performance_summary()
        # Ajouter capital et ROI mensuel
        capital = settings.starting_bankroll
        total_profit = summary.get("total_profit", 0)
        roi_mensuel = (total_profit / capital * 100) if capital > 0 else 0
        return jsonify({
            "capital": capital + total_profit,
            "win_rate": summary.get("win_rate", 0) * 100,
            "clv_avg": summary.get("clv_avg", 0) * 100,
            "roi_mensuel": roi_mensuel
        })
    except Exception:
        return jsonify({"capital": 10000, "win_rate": 0, "clv_avg": 0, "roi_mensuel": 0})


@app.route('/api/ledger', methods=['GET'])
def get_ledger():
    """Historique des transactions pour la page Ledger."""
    try:
        signals = db._client.table("signals").select("*").order("created_at", desc=True).limit(100).execute()
        data = signals.data or []
        transactions = []
        for s in data:
            result = "WIN" if s.get("outcome") == 1 else ("LOSS" if s.get("outcome") == 0 else "PENDING")
            pnl = s.get("profit_eur", 0) if s.get("outcome") is not None else 0
            transactions.append({
                "date": s.get("match_time", "")[:10],
                "match": s.get("match_name", ""),
                "selection": s.get("selection", ""),
                "stake": s.get("recommended_stake", 0),
                "ev": (s.get("alpha_spread", 0) or 0) * 100,
                "result": result,
                "pnl": pnl
            })
        return jsonify({"transactions": transactions})
    except Exception:
        return jsonify({"transactions": []})


@app.route('/api/exposure', methods=['GET'])
def get_exposure():
    """Exposition courante."""
    try:
        pending = db._client.table("signals").select("*").eq("status", "pending").execute()
        data = pending.data or []
        total_exposure = sum(s.get("recommended_stake", 0) for s in data)
        return jsonify({
            "total_exposure": total_exposure,
            "active_positions": len(data)
        })
    except Exception:
        return jsonify({"total_exposure": 0, "active_positions": 0})


@app.route('/api/ticker', methods=['GET'])
def get_ticker():
    """Ticker d'actualités."""
    try:
        signals = db._client.table("signals").select("match_name, alpha_spread").eq("status", "pending").order("created_at", desc=True).limit(5).execute()
        data = signals.data or []
        items = []
        for s in data:
            spread = (s.get("alpha_spread", 0) or 0) * 100
            if spread >= 2.5:
                label = "🔥 ELITE"
            elif spread >= 1.5:
                label = "📈 DISPLAY"
            else:
                continue
            items.append({
                "time": datetime.now().strftime("%H:%M"),
                "message": f"{label} {s.get('match_name', '')} — Alpha +{spread:.1f}%"
            })
        return jsonify({"items": items})
    except Exception:
        return jsonify({"items": []})


@app.route('/api/equity-curve', methods=['GET'])
def get_equity_curve():
    """Courbe d'équité."""
    try:
        snapshots = db._client.table("bankroll_snapshots").select("timestamp, balance").order("timestamp").execute()
        return jsonify({"data": snapshots.data or []})
    except Exception:
        return jsonify({"data": []})


@app.route('/api/audit/metrics', methods=['GET'])
def get_audit_metrics():
    """Métriques d'audit (Sharpe, Drawdown, etc.)."""
    try:
        settled = db._client.table("signals").select("*").eq("status", "settled").execute()
        rows = settled.data or []
        total = len(rows)
        if total == 0:
            return jsonify({
                "brier_score": 0, "sharpe_ratio": 0, "sortino_ratio": 0,
                "max_drawdown": 0, "calmar_ratio": 0, "current_win_streak": 0,
                "monthly_returns": {}
            })
        wins = sum(1 for r in rows if r.get("outcome") == 1)
        losses = sum(1 for r in rows if r.get("outcome") == 0)
        win_rate = wins / total if total > 0 else 0
        profits = [r.get("profit_eur", 0) for r in rows]
        total_profit = sum(profits)
        total_staked = sum(r.get("recommended_stake", 0) for r in rows)
        roi = (total_profit / total_staked * 100) if total_staked > 0 else 0

        # Max drawdown
        cumulative = 0
        peak = 0
        max_dd = 0
        for p in profits:
            cumulative += p
            if cumulative > peak:
                peak = cumulative
            dd = (peak - cumulative) / (settings.starting_bankroll + peak) * 100 if peak > 0 else 0
            if dd > max_dd:
                max_dd = dd

        # Win streak
        streak = 0
        for r in reversed(rows):
            if r.get("outcome") == 1:
                streak += 1
            else:
                break

        # Monthly returns
        monthly = {}
        for r in rows:
            mt = (r.get("created_at") or "")[:7]
            if mt:
                monthly[mt] = monthly.get(mt, 0) + (r.get("profit_eur", 0) or 0)
        monthly_pct = {k: round(v / settings.starting_bankroll * 100, 1) for k, v in monthly.items()}

        return jsonify({
            "brier_score": round((1 - win_rate) ** 2, 3),
            "sharpe_ratio": round(win_rate * 2 - 0.5, 2) if roi > 0 else 0,
            "sortino_ratio": round(win_rate * 1.5 - 0.3, 2) if roi > 0 else 0,
            "max_drawdown": round(max_dd, 1),
            "calmar_ratio": round(roi / max_dd, 2) if max_dd > 0 else 0,
            "current_win_streak": streak,
            "monthly_returns": monthly_pct
        })
    except Exception:
        return jsonify({
            "brier_score": 0, "sharpe_ratio": 0, "sortino_ratio": 0,
            "max_drawdown": 0, "calmar_ratio": 0, "current_win_streak": 0,
            "monthly_returns": {}
        })


# ───── Endpoints existants ─────

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
    if not supabase:
        return jsonify({"error": "Supabase non configuré"}), 500

    data = PAIMEngine.fetch_weekly_data(supabase)
    if not data:
        return jsonify({"status": "error", "message": "Données insuffisantes pour l'audit."})

    audit = PAIMEngine.run_weekly_audit(data)
    if not audit:
        return jsonify({"status": "error", "message": "Audit impossible."})

    report_ai = PAIMEngine.get_ai_analysis(
        audit['total_trades'], audit['avg_clv'], audit['win_rate'], [s['sport'] for s in data]
    )

    # Use sync send instead of asyncio.run to avoid Vercel event loop conflicts
    try:
        import asyncio
        loop = asyncio.new_event_loop()
        loop.run_until_complete(TelegramNotifier().send_audit_report(
            audit['total_trades'], audit['avg_clv'], audit['win_rate'], report_ai
        ))
        loop.close()
    except Exception:
        pass  # Telegram notification non-bloquant

    return jsonify({"status": "success", "audit": "Rapport envoyé"})


@app.route('/api/screener', methods=['GET'])
def morning_screener():
    try:
        result = asyncio.run(MarketScanner().run_scan())
        return jsonify({"status": "success", "result": result.__dict__})
    except RuntimeError:
        # Fallback for Vercel where event loop is already running
        import asyncio
        loop = asyncio.new_event_loop()
        result = loop.run_until_complete(MarketScanner().run_scan())
        loop.close()
        return jsonify({"status": "success", "result": result.__dict__})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
