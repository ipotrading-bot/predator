"""
api/index.py — PREDATOR PAIM v7.5 — Vercel Dashboard (Guerrilla Mode)
Read-only: fetches signals + engine heartbeat from Supabase.
Sources: 1XBet Harvester (Soft) + Gemini/Pinnacle (Sharp). No Odds API.
"""
import json
import os

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template
from supabase import create_client

load_dotenv()

_template_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "templates")
app = Flask(__name__, template_folder=_template_dir)


def _db():
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_KEY")
    return create_client(url, key) if url and key else None


def _get_meta(sb, key: str) -> dict | None:
    try:
        res = sb.table("meta").select("value").eq("key", key).limit(1).execute()
        if res.data:
            return json.loads(res.data[0]["value"])
    except Exception:
        pass
    return None


@app.route("/")
def dashboard():
    signals = []
    last_scan = None
    try:
        sb = _db()
        if sb:
            res = sb.table("signals").select("*").order("created_at", desc=True).limit(50).execute()
            signals = res.data or []
            last_scan = _get_meta(sb, "last_scan")
    except Exception as e:
        print(f"[Dashboard] {e}")
    return render_template("index.html", signals=signals, last_scan=last_scan)


@app.route("/api/signals")
def api_signals():
    try:
        sb = _db()
        if not sb:
            return jsonify({"error": "no db"}), 503
        res = sb.table("signals").select("*").order("created_at", desc=True).limit(50).execute()
        return jsonify(res.data or [])
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "version": "7.5-guerrilla", "source": "harvester+gemini"})
