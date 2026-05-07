"""
api/index.py — Predator PAIM v3.0 (Emergency Debug Mode)
Flask application déployée sur Vercel.
"""
import os
import sys
import traceback
from flask import Flask, render_template, jsonify

# ── Configuration du chemin des templates pour Vercel ──────────
# Le dossier templates est à la racine du projet, pas dans api/
template_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'templates'))
app = Flask(__name__, template_folder=template_dir)


# ── Routes ────────────────────────────────────────────────────

@app.route('/')
def index():
    try:
        # Test de présence du fichier template
        template_path = os.path.join(template_dir, 'index.html')
        if not os.path.exists(template_path):
            return f"""
            <h1>ERREUR : Template introuvable</h1>
            <p>Cherché dans : <code>{template_path}</code></p>
            <p>Contenu de <code>{template_dir}</code> : {os.listdir(template_dir) if os.path.exists(template_dir) else 'DOSSIER MANQUANT'}</p>
            <p>CWD : <code>{os.getcwd()}</code></p>
            <p>sys.path : <code>{sys.path}</code></p>
            """, 500
        return render_template('index.html')
    except Exception as e:
        error_trace = traceback.format_exc()
        return f"<pre>ERREUR CRITIQUE FLASK :\n{error_trace}</pre>", 500


@app.route('/api/health')
def health():
    """Health check avec diagnostic des variables d'environnement."""
    try:
        from config import settings
        config_ok = True
    except Exception as e:
        config_ok = False
        config_error = str(e)

    return jsonify({
        "status": "ok",
        "python_version": sys.version,
        "template_dir": template_dir,
        "template_exists": os.path.exists(os.path.join(template_dir, 'index.html')),
        "config_loaded": config_ok,
        "env_vars": {
            "ODDS_API_KEY": "SET" if os.environ.get("ODDS_API_KEY") else "MISSING",
            "SUPABASE_URL": "SET" if os.environ.get("SUPABASE_URL") else "MISSING",
            "SUPABASE_KEY": "SET" if os.environ.get("SUPABASE_KEY") else "MISSING",
            "GEMINI_API_KEY": "SET" if os.environ.get("GEMINI_API_KEY") else "MISSING",
            "TELEGRAM_BOT_TOKEN": "SET" if os.environ.get("TELEGRAM_BOT_TOKEN") else "MISSING",
            "TELEGRAM_CHAT_ID": "SET" if os.environ.get("TELEGRAM_CHAT_ID") else "MISSING",
            "NEWS_API_KEY": "SET" if os.environ.get("NEWS_API_KEY") else "MISSING",
        }
    })


@app.route('/api/screener')
def screener():
    try:
        # Votre logique de scan
        return jsonify({"status": "success", "items": []})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/data')
def get_data():
    return jsonify([])


@app.route('/api/stats')
def get_stats():
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
    return jsonify([])


@app.route('/api/info')
def get_info_pages_api():
    return jsonify([])


@app.route('/api/audit', methods=['POST'])
def audit_settlement():
    return jsonify({"status": "success", "message": "Aucun signal à régler."})


@app.route('/api/audit/weekly')
def weekly_performance_audit():
    return jsonify({"status": "success", "audit": "Rapport envoyé"})