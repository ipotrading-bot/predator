"""
api/index.py — Predator PAIM v3.0
Flask application déployée sur Vercel.
"""
import logging
import os
import sys
import json
import traceback
from datetime import datetime
from flask import Flask, render_template, jsonify, request
from data.supabase_client import SupabaseClient

logger = logging.getLogger(__name__)

# ── Configuration Vercel ──────────────────────────────────────
template_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'templates'))
app = Flask(__name__, template_folder=template_dir)

# Lazy-init DB — évite crash si Supabase non configuré au démarrage Vercel
_db = None
def get_db() -> SupabaseClient:
    global _db
    if _db is None:
        _db = SupabaseClient()
    return _db

# Seuil de détection : 1.5% display, 2.5% elite
ALPHA_DISPLAY_MIN = 0.015
ALPHA_ELITE_MIN   = 0.025


# ── Route principale ──────────────────────────────────────────

@app.route('/')
def index():
    try:
        template_path = os.path.join(template_dir, 'index.html')
        if not os.path.exists(template_path):
            return f"Template introuvable: {template_path}", 500
        return render_template('index.html')
    except Exception as e:
        return f"<pre>ERREUR:\n{traceback.format_exc()}</pre>", 500


# ── Health Check avec diagnostic ──────────────────────────────

@app.route('/api/health')
def health():
    try:
        from config import settings
        config_ok = True
    except Exception as e:
        config_ok = False

    supabase_ok = False
    try:
        db = get_db()
        db._client.table("signals").select("count", count="exact").limit(1).execute()
        supabase_ok = True
    except Exception:
        pass

    return jsonify({
        "status": "ok",
        "config_loaded": config_ok,
        "supabase_connected": supabase_ok,
        "alpha_display_min": ALPHA_DISPLAY_MIN,
        "alpha_elite_min": ALPHA_ELITE_MIN,
        "env_vars": {
            "ODDS_API_KEY":    "SET" if os.environ.get("ODDS_API_KEY") else "MISSING",
            "SUPABASE_URL":    "SET" if os.environ.get("SUPABASE_URL") else "MISSING",
            "GEMINI_API_KEY":  "SET" if os.environ.get("GEMINI_API_KEY") else "MISSING",
            "GROQ_API_KEY":    "SET" if os.environ.get("GROQ_API_KEY") else "MISSING",
            "NEWS_API_KEY":    "SET" if os.environ.get("NEWS_API_KEY") else "MISSING",
        }
    })


# ── Screener avec logs de debug ───────────────────────────────

@app.route('/api/screener')
def screener():
    """Déclenche un scan PAIM complet."""
    try:
        from config import settings
        from signals.scanner import MarketScanner
        import asyncio

        scanner = MarketScanner(bankroll=settings.starting_bankroll)
        # Seuil abaissé à 1.5% pour le display (2.5% pour elite)
        scanner.engine.min_ev_threshold = ALPHA_DISPLAY_MIN

        loop = asyncio.new_event_loop()
        result = loop.run_until_complete(scanner.run_scan())
        loop.close()

        result_data = result.__dict__ if hasattr(result, '__dict__') else {}
        return jsonify({
            "status": "success",
            "result": result_data,
            "threshold_used": ALPHA_DISPLAY_MIN
        })

    except ImportError as e:
        logger.error(f"ImportError screener: {e}")
        return jsonify({"status": "error", "message": f"ImportError: {str(e)}"}), 500
    except Exception as e:
        logger.error(f"Erreur screener: {traceback.format_exc()}")
        return jsonify({"status": "error", "message": str(e)}), 500


# ── Route test seed (données factices pour valider le frontend) ──

@app.route('/api/test-seed')
def test_seed():
    """Génère des données de test pour valider le Dashboard."""
    from datetime import datetime, timedelta
    import random

    test_signals = []
    sports = ["basketball_nba", "soccer_epl", "esports_lol", "soccer_uefa_champs_league"]
    teams_pool = [
        ("Lakers", "Celtics"), ("Real Madrid", "Barcelona"), ("Manchester City", "Arsenal"),
        ("PSG", "Bayern"), ("Dallas Mavericks", "Boston Celtics"), ("T1", "Gen.G"),
        ("PSG", "Real Madrid"), ("Inter", "Milan"), ("Juventus", "AC Milan"),
        ("Golden State", "Denver"), ("Barcelona", "Atletico"), ("Arsenal", "Chelsea"),
    ]

    for i in range(9):  # 9 signaux (système 7/9)
        home, away = random.choice(teams_pool)
        sharp_prob = round(random.uniform(0.40, 0.65), 3)
        soft_odds = round(random.uniform(1.80, 3.50), 2)
        implied_prob = round(1.0 / soft_odds, 3)
        ev = round(sharp_prob * soft_odds - 1.0, 4)
        alpha_pct = round(ev * 100, 1)
        stake = random.choice([10, 20, 30, 40, 50])
        match_time = (datetime.now() + timedelta(hours=random.randint(2, 24))).isoformat()

        test_signals.append({
            "id": f"test-{i+1}",
            "event_id": f"test-event-{i+1}",
            "match_name": f"{home} vs {away}",
            "sport": random.choice(sports),
            "match_time": match_time,
            "market_type": "h2h" if "nba" not in home.lower() else "spreads",
            "selection": home,
            "bookmaker_target": "onexbet",
            "sharp_prob": sharp_prob,
            "implied_prob_soft": implied_prob,
            "alpha_spread": ev,
            "snr_ratio": round(ev / max(abs(implied_prob - sharp_prob), 0.001), 2),
            "recommended_stake": stake,
            "clv_estimate": round((sharp_prob - implied_prob) / implied_prob, 4),
            "ai_context": "✅ Test seed - Aucun risque majeur détecté.",
            "is_elite": ev >= 0.025,
            "status": "pending",
            "created_at": datetime.now().isoformat(),
            # Champs pour l'affichage dashboard
            "fair_price": round(1.0 / sharp_prob, 2),
            "cote_1xbet": soft_odds,
            "note_ia": "🧪 SIGNAL DE TEST - Données simulées pour validation Dashboard",
        })

    return jsonify({
        "status": "success",
        "count": len(test_signals),
        "signals": test_signals
    })


# ── Endpoints dashboard (données réelles Supabase avec fallback test) ──

@app.route('/api/data')
def get_data():
    try:
        db = get_db()
        signals = db._client.table("signals").select("*").eq("status", "pending").order("created_at", desc=True).limit(50).execute()
        data = signals.data or []

        # Enrichir chaque signal avec les champs calculés manquants
        for sig in data:
            # fair_price depuis sharp_prob si absent
            if not sig.get("fair_price") and sig.get("sharp_prob"):
                sig["fair_price"] = round(1.0 / sig["sharp_prob"], 3)
            # cote_1xbet depuis implied_prob_soft si absent
            if not sig.get("cote_1xbet") and sig.get("implied_prob_soft"):
                sig["cote_1xbet"] = round(1.0 / sig["implied_prob_soft"], 3)
            # note_ia depuis ai_context si absent
            if not sig.get("note_ia") and sig.get("ai_context"):
                sig["note_ia"] = sig["ai_context"]
            # badge elite
            sig["is_elite"] = (sig.get("alpha_spread") or 0) >= ALPHA_ELITE_MIN

        if not data:
            from datetime import timedelta
            match_time = (datetime.now() + timedelta(hours=4)).isoformat()
            data = [{
                "match_name": "📡 Aucun signal actif",
                "sport": "—",
                "match_time": match_time,
                "market_type": "—",
                "alpha_spread": 0,
                "recommended_stake": 0,
                "fair_price": 0,
                "cote_1xbet": 0,
                "note_ia": "📡 SCAN PAIM EN COURS — RECHERCHE D'ALPHA > 1.5%",
                "sources_validated": "",
                "is_elite": False,
            }]
        return jsonify(data)
    except Exception as e:
        logger.error(f"Erreur /api/data: {e}")
        return jsonify([])


@app.route('/api/stats')
def get_stats():
    try:
        db = get_db()
        summary = db.get_performance_summary()
        from config import settings
        capital = settings.starting_bankroll
        total_profit = summary.get("total_profit", 0) or 0
        win_rate = (summary.get("win_rate", 0) or 0) * 100
        clv_avg = (summary.get("clv_avg", 0) or 0) * 100
        roi = (total_profit / capital * 100) if capital > 0 else 0
        return jsonify({
            "capital": round(capital + total_profit, 2),
            "win_rate": round(win_rate, 1),
            "clv_avg": round(clv_avg, 2),
            "roi_mensuel": round(roi, 1),
        })
    except Exception as e:
        logger.error(f"Erreur /api/stats: {e}")
        return jsonify({"capital": 10000, "win_rate": 0, "clv_avg": 0, "roi_mensuel": 0})


@app.route('/api/ledger')
def get_ledger():
    return jsonify({"transactions": []})


@app.route('/api/exposure')
def get_exposure():
    return jsonify({"total_exposure": 0, "active_positions": 0})


@app.route('/api/ticker')
def get_ticker():
    return jsonify({"items": []})


@app.route('/api/equity-curve')
def get_equity_curve():
    return jsonify({"data": []})


@app.route('/api/audit/metrics')
def get_audit_metrics():
    return jsonify({
        "brier_score": 0, "sharpe_ratio": 0, "sortino_ratio": 0,
        "max_drawdown": 0, "calmar_ratio": 0, "current_win_streak": 0,
        "monthly_returns": {}
    })


@app.route('/api/search')
def search_signals_api():
    date = request.args.get('date')
    time_param = request.args.get('time')
    sport = request.args.get('sport')
    try:
        db = get_db()
        results = db.search_signals(sport=sport, date=date, time=time_param)
    except Exception as e:
        logger.error(f"Erreur /api/search: {e}")
        results = []
    return jsonify(results)


@app.route('/api/info')
def get_info_pages_api():
    try:
        db = get_db()
        results = db.get_info_pages()
    except Exception as e:
        logger.error(f"Erreur /api/info: {e}")
        results = []
    return jsonify(results)


@app.route('/api/audit', methods=['POST'])
def audit_settlement():
    from api.audit import run_settlement_audit
    result = run_settlement_audit()
    return jsonify(result)


@app.route('/api/healthcheck')
def healthcheck():
    return jsonify({"status": "ok", "version": "3.0.0", "ts": int(datetime.now().timestamp())})


# ── Market Heatmap (Alpha moyen par sport) ────────────────────────

@app.route('/api/heatmap')
def get_market_heatmap():
    try:
        from datetime import timedelta
        db = get_db()
        cutoff = (datetime.now() - timedelta(hours=24)).isoformat()
        signals = db._client.table("signals").select("sport, alpha_spread").gte("created_at", cutoff).execute()
        data = signals.data or []
        if not data:
            return jsonify({"heatmap": {}})
        sport_alphas: dict = {}
        for sig in data:
            sport = sig.get("sport", "unknown")
            alpha = sig.get("alpha_spread")
            if alpha is not None and isinstance(alpha, (int, float)):
                sport_alphas.setdefault(sport, []).append(float(alpha))
        heatmap = {
            sport: round(sum(alphas) / len(alphas) * 100, 2)
            for sport, alphas in sport_alphas.items() if alphas
        }
        return jsonify({"heatmap": heatmap})
    except Exception as e:
        logger.error(f"Erreur heatmap: {e}")
        return jsonify({"heatmap": {}})


# ── CLV Counter (10 derniers signaux) ─────────────────────────────

@app.route('/api/clv-counter')
def get_clv_counter():
    try:
        db = get_db()
        signals = (
            db._client.table("signals")
            .select("clv_estimate")
            .eq("status", "settled")
            .order("settled_at", desc=True)
            .limit(10)
            .execute()
        )
        data = signals.data or []
        clv_values = [
            float(s["clv_estimate"]) for s in data
            if s.get("clv_estimate") is not None
        ]
        clv_avg = sum(clv_values) / len(clv_values) if clv_values else 0
        return jsonify({"clv_avg": round(clv_avg * 100, 2), "count": len(clv_values)})
    except Exception as e:
        logger.error(f"Erreur CLV counter: {e}")
        return jsonify({"clv_avg": 0, "count": 0})


# ── API Test (quota + connexion) ──────────────────────────────────

@app.route('/api/test-connection')
def test_api_connection():
    """Teste la connexion The-Odds-API et retourne le quota restant."""
    try:
        import requests
        from config import settings
        
        url = "https://api.the-odds-api.com/v4/sports"
        params = {"apiKey": settings.odds_api_key}
        
        response = requests.get(url, params=params, timeout=10)
        
        if response.status_code == 200:
            remaining = response.headers.get("x-requests-remaining", "?")
            used = response.headers.get("x-requests-used", "?")
            
            return jsonify({
                "status": "connected",
                "quota_remaining": remaining,
                "quota_used": used,
                "message": f"✅ API connectée | {remaining} requêtes restantes"
            })
        else:
            return jsonify({
                "status": "error",
                "message": f"❌ Erreur API {response.status_code}"
            }), 500
            
    except Exception as e:
        return jsonify({
            "status": "error",
            "message": f"❌ Connexion échouée: {str(e)}"
        }), 500