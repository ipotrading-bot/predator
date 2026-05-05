"""
api/index.py — Vercel Serverless Flask Entry Point v2.1
"""
from flask import Flask, render_template, jsonify, request
import os, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

template_folder = os.path.join(ROOT, 'templates')
app = Flask(__name__, template_folder=template_folder)
app.config['JSON_SORT_KEYS'] = False


def get_supabase():
    try:
        from supabase import create_client
        url = os.environ.get("SUPABASE_URL", "")
        key = os.environ.get("SUPABASE_KEY", "")
        if not url or not key:
            return None
        return create_client(url, key)
    except Exception:
        return None


@app.after_request
def cors(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, X-Predator-Secret'
    return response


@app.route('/', defaults={'path': ''}, methods=['OPTIONS'])
@app.route('/<path:path>', methods=['OPTIONS'])
def options_handler(path=''):
    return jsonify({}), 204


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/health')
def get_health():
    db = "online" if get_supabase() else "disconnected"
    return jsonify({
        "status": "ok",
        "version": "2.1.0",
        "timestamp": int(time.time()),
        "db": db
    })


@app.route('/api/stats')
def get_stats():
    try:
        supabase = get_supabase()
        if not supabase:
            raise Exception("Supabase non configuré")

        res = supabase.table('signals').select('*').execute()
        data = res.data or []
        total = len(data)

        # CORRECTIF : outcome est SMALLINT 1/0/-1, pas string 'win'
        wins = sum(1 for s in data if s.get('outcome') == 1)
        win_rate = (wins / total * 100) if total > 0 else 0

        clv_vals = [s['clv_estimate'] for s in data if s.get('clv_estimate')]
        clv_avg = (sum(clv_vals) / len(clv_vals)) if clv_vals else 0

        total_profit = sum(s.get('profit_eur', 0) or 0 for s in data)
        starting = float(os.environ.get("STARTING_BANKROLL", "10000"))

        return jsonify({
            "capital": round(starting + total_profit, 2),
            "win_rate": round(win_rate, 1),
            "clv_avg": round(clv_avg * 100, 2),
            "roi_mensuel": round(total_profit / starting * 100, 1),
            "total_signals": total,
            "winning_signals": wins
        })
    except Exception as e:
        return jsonify({
            "error": str(e),
            "capital": 10000, "win_rate": 0,
            "clv_avg": 0, "roi_mensuel": 0,
            "total_signals": 0, "winning_signals": 0
        })


@app.route('/api/signals/live')
def get_live_signals():
    try:
        supabase = get_supabase()
        if not supabase:
            return jsonify({"signals": [], "count": 0})
        res = (supabase.table('signals')
               .select('*')
               .eq('status', 'pending')
               .order('ev_plus', desc=True)
               .limit(9)
               .execute())
        return jsonify({"signals": res.data or [], "count": len(res.data or [])})
    except Exception as e:
        return jsonify({"signals": [], "count": 0, "error": str(e)})


@app.route('/api/scan', methods=['POST', 'GET'])
def trigger_scan():
    import threading, asyncio
    session = (request.json or {}).get('session', 'api')
    box = {}

    def run():
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            from config import settings
            from signals.scanner import MarketScanner
            scanner = MarketScanner(bankroll=settings.starting_bankroll)
            r = loop.run_until_complete(scanner.run_scan())
            loop.close()
            box['ok'] = {
                "success": True,
                "session": session,
                "events_analyzed": r.events_analyzed,
                "signals_validated": r.signals_validated,
                "duration_seconds": round(r.duration_seconds, 2),
                "timestamp": int(time.time())
            }
        except Exception as e:
            box['err'] = str(e)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout=55)

    if 'ok' in box:
        return jsonify(box['ok'])
    if 'err' in box:
        return jsonify({"success": False, "error": box['err']}), 500
    return jsonify({"success": False, "error": "Scan timeout"}), 504


@app.route('/api/exposure')
def get_current_exposure():
    try:
        supabase = get_supabase()
        if not supabase:
            raise Exception("DB not configured")
        res = (supabase.table('signals')
               .select('recommended_stake')
               .eq('status', 'pending')
               .execute())
        data = res.data or []
        exp = sum(s.get('recommended_stake', 0) or 0 for s in data)
        starting = float(os.environ.get("STARTING_BANKROLL", "10000"))
        return jsonify({
            "total_exposure": round(exp, 2),
            "active_positions": len(data),
            "exposure_percentage": round(exp / starting * 100, 1)
        })
    except Exception as e:
        return jsonify({"total_exposure": 0, "active_positions": 0,
                        "exposure_percentage": 0, "error": str(e)})


@app.route('/api/audit/metrics')
def get_audit_metrics():
    try:
        from api.analytics import quant_analytics
        return jsonify(quant_analytics.get_performance_report(days=30))
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route('/api/equity-curve')
def get_equity_curve():
    try:
        supabase = get_supabase()
        if not supabase:
            raise Exception("DB not configured")
        res = (supabase.table("bankroll_snapshots")
               .select("timestamp,balance,roi,drawdown")
               .order("timestamp")
               .limit(200)
               .execute())
        return jsonify({"data": res.data or []})
    except Exception as e:
        return jsonify({"data": [], "error": str(e)})


@app.route('/api/ledger')
def get_ledger():
    try:
        supabase = get_supabase()
        if not supabase:
            raise Exception("DB not configured")
        res = (supabase.table("signals")
               .select("event_name,recommended_stake,ev_plus,outcome,profit_eur,created_at,selection")
               .not_.is_("outcome", "null")
               .order("created_at", desc=True)
               .limit(50)
               .execute())
        om = {1: "WIN", 0: "LOSS", -1: "VOID"}
        ledger = [{
            "date": s.get("created_at", "")[:10],
            "match": s.get("event_name", "N/A"),
            "selection": s.get("selection", ""),
            "stake": s.get("recommended_stake", 0),
            "ev": round((s.get("ev_plus", 0) or 0) * 100, 1),
            "result": om.get(s.get("outcome"), "N/A"),
            "pnl": s.get("profit_eur", 0)
        } for s in (res.data or [])]
        return jsonify({"transactions": ledger})
    except Exception as e:
        return jsonify({"transactions": [], "error": str(e)})


@app.route('/api/ticker')
def get_market_ticker():
    try:
        from api.news_client import news_client
        return jsonify({"items": news_client.get_ticker_items()})
    except Exception as e:
        return jsonify({"items": [], "error": str(e)})


@app.route('/api/logs/recent')
def get_recent_logs():
    try:
        from api.logger import get_scan_logger
        lg = get_scan_logger()
        return jsonify(lg.get_summary() if lg else {"steps": []})
    except Exception as e:
        return jsonify({"steps": [], "error": str(e)})


# ── VERCEL : pas de handler() ici ────────────────────────────────────
# @vercel/python détecte l'objet Flask 'app' automatiquement.
# L'ancienne ligne "def handler(request): return app(request.environ, lambda *args: None)"
# provoquait le 500 en détruisant start_response.