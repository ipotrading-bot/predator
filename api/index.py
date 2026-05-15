"""
api/index.py — PREDATOR PAIM v8.5 — Vercel Dashboard + Ledger
Routes: / (Dashboard)  /ledger (CLV Bilan)  /api/signals  /api/health  /api/scan
"""
import json
import os

import requests
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


# ── Dashboard ────────────────────────────────────────────────────────

@app.route("/")
def dashboard():
    signals   = []
    last_scan = None
    try:
        sb = _db()
        if sb:
            res = sb.table("signals").select("*").order("created_at", desc=True).limit(50).execute()
            signals   = res.data or []
            last_scan = _get_meta(sb, "last_scan")
    except Exception as e:
        print(f"[Dashboard] {e}")
    return render_template("index.html", signals=signals, last_scan=last_scan)


# ── Ledger ───────────────────────────────────────────────────────────

@app.route("/ledger")
def ledger():
    signals    = []
    stats: dict = {}
    try:
        sb = _db()
        if sb:
            # Closed/expired signals sorted by CLV (best first)
            res = (sb.table("signals")
                   .select("*")
                   .in_("status", ["closed", "expired"])
                   .order("clv_pct", desc=True)
                   .limit(200)
                   .execute())
            signals = [s for s in (res.data or []) if s.get("clv_pct") is not None]

            if signals:
                clv_vals  = [s["clv_pct"] for s in signals]
                hit_count = sum(1 for c in clv_vals if c >= 0)
                stats = {
                    "total":     len(signals),
                    "hit_rate":  round(hit_count / len(clv_vals) * 100, 1),
                    "avg_clv":   round(sum(clv_vals) / len(clv_vals), 2),
                    "best_clv":  round(max(clv_vals), 2),
                    "worst_clv": round(min(clv_vals), 2),
                }

                # Per-sport breakdown + dynamic thresholds
                t_res = sb.table("meta").select("key,value").like("key", "threshold_%").execute()
                thresholds = {}
                for row in (t_res.data or []):
                    sport = row["key"].replace("threshold_", "")
                    thresholds[sport] = float(row["value"])

                sports_stats = {}
                for sport in ["soccer", "basketball", "tennis"]:
                    sv = [s["clv_pct"] for s in signals if s.get("sport") == sport]
                    if sv:
                        sports_stats[sport] = {
                            "count":     len(sv),
                            "hit_rate":  round(sum(1 for c in sv if c >= 0) / len(sv) * 100, 1),
                            "avg_clv":   round(sum(sv) / len(sv), 2),
                            "threshold": thresholds.get(sport, 1.5),
                        }
                stats["sports"] = sports_stats

    except Exception as e:
        print(f"[Ledger] {e}")

    return render_template("ledger.html", signals=signals, stats=stats)


# ── JSON API ─────────────────────────────────────────────────────────

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
    return jsonify({"status": "ok", "version": "8.5", "source": "harvester+gemini"})


@app.route("/api/scan", methods=["POST"])
def trigger_scan():
    pat = os.environ.get("GITHUB_PAT")
    if not pat:
        return jsonify({"error": "GITHUB_PAT not configured"}), 503
    try:
        resp = requests.post(
            "https://api.github.com/repos/ipotrading-bot/predator/actions/workflows/engine.yml/dispatches",
            headers={
                "Authorization": f"Bearer {pat}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json={"ref": "main"},
            timeout=10,
        )
        if resp.status_code == 204:
            return jsonify({"status": "triggered"}), 200
        return jsonify({"error": resp.text}), resp.status_code
    except Exception as e:
        return jsonify({"error": str(e)}), 500
