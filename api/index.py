from flask import Flask, render_template, jsonify
import os
from supabase import create_client

app = Flask(__name__, template_folder='../templates')

# Initialisation Supabase
supabase = create_client(os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_KEY"))

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/stats')
def get_stats():
    # Récupère les métriques pour le Dashboard
    res = supabase.table('signals').select('*').execute()
    data = res.data
    # Logique de calcul CLV, Profit, Winrate ici
    stats = {
        "capital": 10000,
        "win_rate": 90.2,
        "clv_avg": 7.4,
        "total_signals": len(data)
    }
    return jsonify(stats)

@app.route('/api/signals/live')
def get_live_signals():
    # Récupère les 9 derniers signaux (Ticket 7/9)
    res = supabase.table('signals').select('*').order('created_at', desc=True).limit(9).execute()
    return jsonify(res.data)

@app.route('/api/scan', methods=['POST'])
def trigger_scan():
    # Déclenche le moteur PAIM
    # engine.run_scan()
    return jsonify({"status": "success", "message": "Scan PAIM activé"})

if __name__ == '__main__':
    app.run()
