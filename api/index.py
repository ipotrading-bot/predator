"""
api/index.py — PREDATOR PAIM v8.5 — Vercel Dashboard + Ledger + Audit
Routes: / (Dashboard)  /ledger (CLV Bilan)  /audit (CLV par sport)
        /api/signals  /api/health  /api/scan
"""
import json
import logging
import os
from datetime import datetime, timezone as _tz

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, send_from_directory
from supabase import create_client

log = logging.getLogger("PREDATOR.api")

load_dotenv()

_template_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "templates")
_static_dir   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
app = Flask(__name__, template_folder=_template_dir, static_folder=_static_dir, static_url_path="/static")


@app.after_request
def no_cache(response):
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"]        = "no-cache"
    response.headers["Expires"]       = "0"
    return response


def _db():
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_KEY")
    return create_client(url, key) if url and key else None


def _get_meta(sb, key: str) -> dict | None:
    try:
        res = (sb.table("meta").select("value,updated_at")
               .eq("key", key)
               .order("updated_at", desc=True)
               .limit(1).execute())
        if res.data:
            return json.loads(res.data[0]["value"])
    except Exception:
        pass
    return None


# ── Dashboard ────────────────────────────────────────────────────────

_DASH_SPORT_ORDER = {
    "basketball": 0, "hockey": 1, "americanfootball": 2, "baseball": 3,
    "esports": 4, "rugby": 5, "tennis": 6, "mma": 7,
    "volleyball": 8, "tabletennis": 9, "handball": 10,
    "boxing": 11, "darts": 12, "cricket": 13, "soccer": 14,
}

_HIGH_QUALITY = {"HIGH_VALUE", "VALUE"}


def _parse_match_time(s: str):
    """Parse match_time to UTC-aware datetime regardless of format (T vs space, Z vs +00:00)."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s[:len(fmt)], fmt).replace(tzinfo=_tz.utc)
        except Exception:
            pass
    return None


@app.route("/")
def dashboard():
    signals   = []
    last_scan = None
    try:
        sb = _db()
        if sb:
            res = sb.table("signals").select("*").order("created_at", desc=True).limit(200).execute()
            raw = res.data or []

            # Deduplicate: keep NEWEST signal per (match_id, market_key).
            # Signals are already ordered created_at DESC so first-seen = freshest.
            seen: dict = {}
            for s in raw:
                mid  = s.get("match_id") or s.get("match", "")
                mkey = s.get("market_key") or s.get("market", "")
                key  = (mid, mkey)
                if key not in seen:
                    seen[key] = s

            # Drop signals whose match has already started (proper datetime parse)
            _now = datetime.now(_tz.utc)
            filtered = [
                s for s in seen.values()
                if s.get("risk_flag") in _HIGH_QUALITY
                and (not s.get("match_time") or (_parse_match_time(s["match_time"]) or _now) > _now)
            ]
            signals = sorted(
                filtered,
                key=lambda s: (
                    _DASH_SPORT_ORDER.get(s.get("sport", ""), 9),
                    s.get("match_time") or "9999",
                    -(s.get("edge_pct") or 0),
                ),
            )
            # Parse sharp_sources JSON string → dict, consensus_score → int
            for s in signals:
                ss = s.get("sharp_sources")
                if isinstance(ss, str):
                    try:
                        s["sharp_sources"] = json.loads(ss)
                    except Exception:
                        s["sharp_sources"] = {}
                elif ss is None:
                    s["sharp_sources"] = {}
                cs = s.get("consensus_score")
                s["consensus_score"] = int(cs) if cs is not None else None
            last_scan = _get_meta(sb, "last_scan")
    except Exception as e:
        log.error("Dashboard: %s", e)
    return render_template("index.html", signals=signals, last_scan=last_scan)


# ── Ledger ───────────────────────────────────────────────────────────

_SPORT_EMOJI = {
    "soccer": "⚽", "basketball": "🏀", "tennis": "🎾", "hockey": "🏒",
    "mma": "🥋", "boxing": "🥊", "darts": "🎯", "cricket": "🏏",
    "esports": "🎮", "americanfootball": "🏈", "baseball": "⚾",
    "rugby": "🏉", "volleyball": "🏐", "tabletennis": "🏓", "handball": "🤾",
}
_SPORT_LABEL = {
    "soccer": "Football", "basketball": "Basket", "tennis": "Tennis",
    "hockey": "Hockey", "mma": "MMA", "boxing": "Boxe", "darts": "Fléchettes",
    "cricket": "Cricket", "esports": "eSports", "americanfootball": "NFL",
    "baseball": "MLB", "rugby": "Rugby", "volleyball": "Volley",
    "tabletennis": "Ping-Pong", "handball": "Handball",
}

def _clv_verdict(avg_clv: float, count: int) -> str:
    """Return BOOST / STABLE / ATTENTION / SUSPENDU based on CLV performance."""
    if count < 3:
        return "INSUFFISANT"
    if avg_clv >= 5.0:
        return "BOOST"
    if avg_clv >= 0.0:
        return "STABLE"
    if avg_clv >= -15.0:
        return "ATTENTION"
    return "SUSPENDU"


@app.route("/ledger")
def ledger():
    signals    = []
    stats: dict = {}
    try:
        sb = _db()
        if sb:
            res = (sb.table("signals")
                   .select("*")
                   .in_("status", ["settled", "closed", "expired"])
                   .order("clv_pct", desc=True)
                   .limit(300)
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

                # Load current dynamic thresholds
                t_res = sb.table("meta").select("key,value").like("key", "threshold_%").execute()
                thresholds = {}
                for row in (t_res.data or []):
                    sport = row["key"].replace("threshold_", "")
                    thresholds[sport] = float(row["value"])

                _DEFAULT_T = {
                    "soccer": 2.5, "basketball": 2.0, "tennis": 1.8, "hockey": 2.0,
                    "mma": 2.5, "boxing": 2.5, "darts": 2.0, "cricket": 2.0,
                    "esports": 2.2, "americanfootball": 2.0, "baseball": 2.0,
                    "rugby": 2.0, "volleyball": 2.0, "tabletennis": 2.0, "handball": 2.0,
                }

                sports_stats = {}
                threshold_updates = []

                # All sports present in signals
                all_sports = sorted(set(s.get("sport", "") for s in signals if s.get("sport")))
                for sport in all_sports:
                    sv = [s["clv_pct"] for s in signals if s.get("sport") == sport]
                    if not sv:
                        continue
                    avg = round(sum(sv) / len(sv), 2)
                    hit = round(sum(1 for c in sv if c >= 0) / len(sv) * 100, 1)
                    verdict = _clv_verdict(avg, len(sv))
                    cur_t   = thresholds.get(sport, _DEFAULT_T.get(sport, 2.0))
                    def_t   = _DEFAULT_T.get(sport, 2.0)

                    # Auto-adjust threshold based on CLV verdict
                    new_t = cur_t
                    if verdict == "SUSPENDU" and cur_t < 5.0:
                        new_t = min(5.0, round(cur_t + 0.5, 1))
                    elif verdict == "BOOST" and len(sv) >= 5 and cur_t > 1.5:
                        new_t = max(1.5, round(cur_t - 0.2, 1))

                    if new_t != cur_t:
                        threshold_updates.append((sport, new_t))

                    sports_stats[sport] = {
                        "count":     len(sv),
                        "hit_rate":  hit,
                        "avg_clv":   avg,
                        "threshold": cur_t,
                        "default_t": def_t,
                        "verdict":   verdict,
                        "emoji":     _SPORT_EMOJI.get(sport, "🎯"),
                        "label":     _SPORT_LABEL.get(sport, sport.capitalize()),
                    }

                # Push threshold updates to Supabase meta
                for sport, new_t in threshold_updates:
                    try:
                        sb.table("meta").upsert({
                            "key":   f"threshold_{sport}",
                            "value": str(new_t),
                        }).execute()
                        log.info("Ledger: threshold %s → %.1f%%", sport, new_t)
                    except Exception as e:
                        log.warning("Ledger threshold update %s: %s", sport, e)

                stats["sports"] = sports_stats

    except Exception as e:
        log.error("Ledger: %s", e)

    return render_template("ledger.html", signals=signals, stats=stats)


# ── JSON API ─────────────────────────────────────────────────────────

@app.route("/audit")
def audit():
    audit_data: dict     = {}
    thresholds: dict     = {}
    recent_signals: list = []
    global_stats: dict   = {}
    try:
        sb = _db()
        if sb:
            res = (sb.table("signals")
                   .select("sport,clv_pct,edge_pct,scanned_at,closed_at,status,match,market,outcome,sharp_prob")
                   .in_("status", ["settled", "closed", "expired"])
                   .order("closed_at", desc=True)
                   .limit(300)
                   .execute())
            rows = [r for r in (res.data or []) if r.get("clv_pct") is not None]

            for sport in ["basketball", "tennis", "soccer", "mma", "boxing", "darts", "cricket"]:
                sv = [r["clv_pct"] for r in rows if r.get("sport") == sport]
                if sv:
                    hits = sum(1 for c in sv if c >= 0)
                    audit_data[sport] = {
                        "count":    len(sv),
                        "hit_rate": round(hits / len(sv) * 100, 1),
                        "avg_clv":  round(sum(sv) / len(sv), 2),
                        "best":     round(max(sv), 2),
                        "worst":    round(min(sv), 2),
                        "recent":   sv[:10],
                    }

            t_res = sb.table("meta").select("key,value").like("key", "threshold_%").execute()
            for row in (t_res.data or []):
                sport = row["key"].replace("threshold_", "")
                thresholds[sport] = float(row["value"])

            recent_signals = rows[:20]

            # ── Global KPIs ──────────────────────────────────────────────
            settled = [r for r in rows if r.get("outcome") in ("WIN", "LOSS", "PUSH")]
            wins    = sum(1 for r in settled if r["outcome"] == "WIN")
            losses  = sum(1 for r in settled if r["outcome"] == "LOSS")
            pushes  = sum(1 for r in settled if r["outcome"] == "PUSH")
            decisive = wins + losses
            global_stats["total"]    = len(rows)
            global_stats["settled"]  = len(settled)
            global_stats["wins"]     = wins
            global_stats["losses"]   = losses
            global_stats["pushes"]   = pushes
            global_stats["hit_rate"] = round(wins / decisive * 100, 1) if decisive else None
            global_stats["avg_clv"]  = round(sum(r["clv_pct"] for r in rows) / len(rows), 2) if rows else None

            # Brier Score: BS = mean((sharp_prob - outcome_binary)²)
            _outcome_map = {"WIN": 1.0, "LOSS": 0.0, "PUSH": 0.5}
            bs_rows = [r for r in settled if r.get("sharp_prob") and r.get("sharp_prob") > 0]
            if bs_rows:
                global_stats["brier"] = round(
                    sum((r["sharp_prob"] - _outcome_map[r["outcome"]]) ** 2 for r in bs_rows)
                    / len(bs_rows),
                    4,
                )

    except Exception as e:
        log.error("Audit: %s", e)

    return render_template("audit.html",
                           audit_data=audit_data,
                           thresholds=thresholds,
                           recent_signals=recent_signals,
                           global_stats=global_stats)


# ── JSON API ─────────────────────────────────────────────────────────

_WC_KEYWORDS = ["world cup", "fifa", "wc 2026", "mondial", "coupe du monde"]


def _is_wc_signal(s: dict) -> bool:
    league = (s.get("league") or "").lower()
    sport  = (s.get("sport")  or "").lower()
    return (
        any(kw in league for kw in _WC_KEYWORDS)
        or "soccer_fifa_world_cup" in sport
    )


# ── World Cup Terminal ────────────────────────────────────────────────

@app.route("/worldcup")
def worldcup():
    signals   = []
    last_scan = None
    try:
        sb = _db()
        if sb:
            res = sb.table("signals").select("*").order("created_at", desc=True).limit(200).execute()
            raw = res.data or []
            signals = sorted(
                [s for s in raw if _is_wc_signal(s)],
                key=lambda s: (
                    s.get("match_time") or "9999",
                    -(s.get("edge_pct") or 0),
                ),
            )
            last_scan = _get_meta(sb, "last_scan")
    except Exception as e:
        log.error("WorldCup: %s", e)
    return render_template("worldcup.html", signals=signals, last_scan=last_scan)


@app.route("/api/worldcup")
def api_worldcup():
    try:
        sb = _db()
        if not sb:
            return jsonify({"error": "no db"}), 503
        res = sb.table("signals").select("*").order("created_at", desc=True).limit(200).execute()
        raw = res.data or []
        wc  = [s for s in raw if _is_wc_signal(s)]
        return jsonify(wc)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


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


@app.route("/api/audit/run", methods=["POST"])
def trigger_audit():
    pat = os.environ.get("GITHUB_PAT")
    if not pat:
        return jsonify({"error": "GITHUB_PAT not configured"}), 503
    try:
        resp = requests.post(
            "https://api.github.com/repos/ipotrading-bot/predator/actions/workflows/audit.yml/dispatches",
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


@app.route("/manifest.json")
def manifest():
    return send_from_directory(_static_dir, "manifest.json", mimetype="application/manifest+json")


@app.route("/favicon.ico")
def favicon():
    return send_from_directory(_static_dir, "favicon.ico", mimetype="image/x-icon")


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
