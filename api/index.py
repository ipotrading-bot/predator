"""
api/index.py — Predator PAIM v3.0 (Mode Diagnostic Screener)
Flask application déployée sur Vercel.
"""
import os
import sys
import json
import traceback
from datetime import datetime
from flask import Flask, render_template, jsonify, request
from data.supabase_client import SupabaseClient

# ── Configuration Vercel ──────────────────────────────────────
template_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'templates'))
app = Flask(__name__, template_folder=template_dir)
db = SupabaseClient()

# ── Seuil abaissé pour test (0.5% au lieu de 2.5%) ────────────
TEST_THRESHOLD = 0.005  # 0.5%


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

    # Tester connexion Supabase
    supabase_ok = False
    try:
        s = db._client.table("signals").select("count", count="exact").limit(1).execute()
        supabase_ok = True
    except:
        pass

    return jsonify({
        "status": "ok",
        "config_loaded": config_ok,
        "supabase_connected": supabase_ok,
        "test_threshold": TEST_THRESHOLD,
        "env_vars": {
            "ODDS_API_KEY": "SET" if os.environ.get("ODDS_API_KEY") else "MISSING",
            "SUPABASE_URL": "SET" if os.environ.get("SUPABASE_URL") else "MISSING",
            "GEMINI_API_KEY": "SET" if os.environ.get("GEMINI_API_KEY") else "MISSING",
        }
    })


# ── Screener avec logs de debug ───────────────────────────────

@app.route('/api/screener')
def screener():
    """Route de scan avec logs de debug et seuil abaissé."""
    try:
        from config import settings
        from signals.scanner import MarketScanner
        import asyncio

        print(f"DEBUG: Démarrage scan avec seuil min_ev={TEST_THRESHOLD}")
        print(f"DEBUG: API Key présente: {bool(settings.odds_api_key)}")
        print(f"DEBUG: Sports cibles: {settings.target_sports}")
        print(f"DEBUG: Books sharp: {settings.sharp_books}")
        print(f"DEBUG: Books soft: {settings.soft_books}")
        print(f"DEBUG: Synonyms config: {settings.synonyms}")

        # Scanner avec bankroll normale, seuil forcé bas
        scanner = MarketScanner(bankroll=10000)
        # Forcer le seuil à 0.5%
        scanner.engine.min_ev_threshold = TEST_THRESHOLD

        loop = asyncio.new_event_loop()
        result = loop.run_until_complete(scanner.run_scan())
        loop.close()

        result_data = result.__dict__ if hasattr(result, '__dict__') else {}
        print(f"DEBUG: Scan terminé: {json.dumps(result_data, default=str)}")

        return jsonify({
            "status": "success",
            "result": result_data,
            "threshold_used": TEST_THRESHOLD
        })

    except ImportError as e:
        print(f"DEBUG IMPORT ERROR: {e}")
        return jsonify({"status": "error", "message": f"ImportError: {str(e)}"}), 500
    except Exception as e:
        print(f"DEBUG SCAN ERROR: {traceback.format_exc()}")
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
        signals = db._client.table("signals").select("*").eq("status", "pending").order("created_at", desc=True).limit(50).execute()
        data = signals.data or []
        if not data:
            # Fallback: retourner les test-seed si la BD est vide
            from datetime import datetime, timedelta
            match_time = (datetime.now() + timedelta(hours=4)).isoformat()
            data = [{
                "match_name": "Lakers vs Celtics",
                "sport": "basketball_nba",
                "match_time": match_time,
                "market_type": "h2h",
                "alpha_spread": 0.031,
                "recommended_stake": 30,
                "fair_price": 1.85,
                "cote_1xbet": 2.10,
                "note_ia": "✅ Test - Aucun signal réel pour le moment. Seuil abaissé à 0.5%."
            }]
        return jsonify(data)
    except Exception as e:
        return jsonify([])


@app.route('/api/stats')
def get_stats():
    try:
        summary = db.get_performance_summary()
        capital = 10000
        total_profit = summary.get("total_profit", 0)
        return jsonify({
            "capital": capital + total_profit,
            "win_rate": (summary.get("win_rate", 0) or 0) * 100,
            "clv_avg": (summary.get("clv_avg", 0) or 0) * 100,
            "roi_mensuel": (total_profit / capital * 100) if capital > 0 else 0
        })
    except:
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
    time = request.args.get('time')
    sport = request.args.get('sport')
    results = db.search_signals(sport=sport, date=date, time=time)
    return jsonify(results)


@app.route('/api/info')
def get_info_pages_api():
    results = db.get_info_pages()
    return jsonify(results)


@app.route('/api/audit', methods=['POST'])
def audit_settlement():
    from api.audit import run_settlement_audit
    result = run_settlement_audit()
    return jsonify(result)


@app.route('/api/healthcheck')
def healthcheck():
    return jsonify({"status": "ok", "version": "3.0.0", "ts": int(datetime.now().timestamp())})