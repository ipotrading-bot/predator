"""
api/index.py — PREDATOR PAIM v8.8 — Vercel Dashboard + Ledger + Audit
Routes: / (Dashboard)  /ledger (CLV Bilan)  /audit (CLV par sport)
        /api/signals  /api/health  /api/scan

Version tag kept in sync with README.md and run_engine.py's own v8.8
header — the single source of truth for the whole-app version number.
Individual core/ modules carry their own, independent per-module version
tags (last significant touch to that file) — those are NOT meant to track
this one.
"""
import json
import logging
import os
from datetime import datetime, timezone as _tz

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, send_from_directory

from core.constants import TAX_RATE as _TAX_RATE
from core.db import get_db as _get_db_client, MissingCredentialsError
from core.odds_api import BASE_URL as _ODDS_BASE_URL
from core.learning_layer import (
    SPORT_DEFAULTS as _SPORT_DEFAULTS,
    load_thresholds as _load_thresholds,
    load_segment_thresholds as _load_segment_thresholds,
    load_learning_summary as _load_learning_summary,
)
from core.risk_manager import is_sport_emission_paused as _is_sport_emission_paused
from core.stats_utils import bucket_predictions, p_breakeven, wilson_ci

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


def _db(write: bool = False):
    """write=False (default, every route except /api/scan): anon key, safe
    for reads, returns None if unconfigured so routes degrade gracefully.
    write=True (/api/scan only): requires SUPABASE_SERVICE_KEY — raises
    MissingCredentialsError if it's absent/wrong, which the one call site
    below turns into an honest error response instead of a silent RLS
    failure on the meta-table upsert."""
    return _get_db_client(write=write)


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

_HIGH_QUALITY = {"HIGH_VALUE", "VALUE", "LOW_VALUE", "SUSPECT_DATA"}


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


def _mk_dash_sort(s: dict, now) -> tuple:
    """Urgence d'abord (< 4h → tier 0 par heure ASC), sinon par sport+edge."""
    mt = _parse_match_time(s.get("match_time") or "")
    if mt:
        secs = (mt - now).total_seconds()
        if 0 < secs <= 14400:
            return (0, s.get("match_time") or "9999", 0, -(s.get("edge_pct") or 0))
    return (1, "0", _DASH_SPORT_ORDER.get(s.get("sport", ""), 9), -(s.get("edge_pct") or 0))


# Même barème que le data-quality du template : 0 = meilleur.
_QUALITY_RANK = {"HIGH_VALUE": 0, "VALUE": 1}


def _group_key(s: dict) -> tuple:
    """Clé de regroupement visuel d'un signal vers sa carte-match.

    On ne se fie pas au seul match_id : le même match réel peut arriver par
    The Odds API (uuid) et par la recherche web (id dérivé des noms d'équipes),
    donc avec deux ids différents. Le nom normalisé + la date de match est ce
    que l'opérateur perçoit comme "le même match".
    """
    return (s.get("sport") or "",
            (s.get("match") or "").lower().strip(),
            (s.get("match_time") or "")[:10])


def _group_by_match(signals: list) -> list:
    """Regrouper les signaux déjà triés en cartes-match.

    Un match génère jusqu'à 3 signaux (h2h + totals + spreads) ; affichés à
    plat, ils se lisent comme des doublons puisque la liste n'affiche que le
    nom du match. Chaque groupe garde l'index plat de ses signaux : le JS
    indexe dans SIGNALS via openModal(idx), cet index doit rester valide.
    """
    groups: dict = {}
    for idx, s in enumerate(signals):
        g = groups.get(_group_key(s))
        if g is None:
            g = {
                "match":        s.get("match") or "",
                "league":       s.get("league") or "",
                "sport":        s.get("sport") or "soccer",
                "match_time":   s.get("match_time") or "",
                "legs":         [],
                "best_edge":    0.0,
                "best_quality": 3,
                "best_flag":    s.get("risk_flag") or "LOW_VALUE",
            }
            groups[_group_key(s)] = g
        g["legs"].append({"idx": idx, "sig": s})
        g["best_edge"] = max(g["best_edge"], s.get("edge_pct") or 0.0)
        rank = _QUALITY_RANK.get(s.get("risk_flag"), 2)
        if rank < g["best_quality"]:
            # La couleur de bordure et le badge de la carte suivent sa
            # meilleure jambe — les signaux sont déjà triés par edge DESC dans
            # un rang donné, donc à rang égal la première jambe vue gagne.
            g["best_quality"] = rank
            g["best_flag"] = s.get("risk_flag") or "LOW_VALUE"
    return list(groups.values())


@app.route("/")
def dashboard():
    signals   = []
    last_scan = None
    try:
        sb = _db()
        if sb:
            res = sb.table("signals").select("*").eq("status", "active").order("created_at", desc=True).limit(200).execute()
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

            _now = datetime.now(_tz.utc)
            # Aucun match déjà commencé sur le dashboard (demande opérateur
            # 2026-07-22) : l'ancienne fenêtre de grâce de 2h après le coup
            # d'envoi gardait des signaux non jouables qui noyaient les
            # signaux encore pariables. Un signal sans match_time reste
            # affiché — on ne peut pas prouver qu'il a commencé.
            filtered = [
                s for s in seen.values()
                if s.get("risk_flag") in _HIGH_QUALITY
                and (not s.get("match_time") or (_parse_match_time(s["match_time"]) or _now) > _now)
            ]
            signals = sorted(filtered, key=lambda s: _mk_dash_sort(s, _now))

            # (Supprimé 2026-07-22) Le fallback « moins de 3 signaux actifs →
            # compléter avec les matchs récemment settlés » remplissait le
            # dashboard de matchs déjà joués. Le filtre ci-dessus n'accepte
            # plus rien après le coup d'envoi : ces lignes étaient de toute
            # façon masquées côté client. L'historique reste sur /bilan.

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
                try:
                    s["consensus_score"] = int(cs) if cs is not None else None
                except (TypeError, ValueError):
                    # A single malformed value here (e.g. a non-integer string)
                    # used to propagate to the outer try/except and blank the
                    # ENTIRE dashboard's signal list, not just this row.
                    s["consensus_score"] = None
            last_scan = _get_meta(sb, "last_scan")
    except Exception as e:
        log.error("Dashboard: %s", e)

    groups = _group_by_match(signals)

    from core.constants import BANKROLL_REF
    return render_template("index.html", signals=signals, groups=groups,
                           last_scan=last_scan, bankroll_ref=BANKROLL_REF)


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

                # All sports present in signals. Display only — this route
                # used to also auto-adjust and upsert thresholds into `meta`,
                # but it runs on the anon key, which RLS has blocked from
                # writing `meta` since migrate_v9_3: the upsert failed on
                # every page view (silent log.warning), and had it ever
                # succeeded it would have fought core/learning_layer.py's
                # adjustment algorithm. Threshold updates belong to the
                # audit workflow (learning_layer), not to a dashboard read.
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
            # ── Active signals — always available ─────────────────────
            try:
                act_res = (sb.table("signals")
                           .select("sport,edge_pct,sharp_prob,kelly_pct,risk_flag,scanned_at,match,market,selection_name,xbet_odd,pinnacle_price")
                           .eq("status", "active")
                           .order("scanned_at", desc=True)
                           .limit(300)
                           .execute())
                active_rows = act_res.data or []
            except Exception as e:
                log.warning("Audit active query: %s", e)
                active_rows = []

            # ── CLV data — only available once settlement is implemented ──
            clv_rows: list = []
            try:
                clv_res = (sb.table("signals")
                           .select("sport,clv_pct,edge_pct,scanned_at,match,market,status")
                           .in_("status", ["settled", "closed"])
                           .not_.is_("clv_pct", "null")
                           .order("scanned_at", desc=True)
                           .limit(200)
                           .execute())
                clv_rows = clv_res.data or []
            except Exception:
                pass

            # ── Per-sport stats from active signals ───────────────────
            for sport in sorted(set(r.get("sport", "") for r in active_rows) - {""}):
                sport_rows = [r for r in active_rows if r.get("sport") == sport]
                edges = [r["edge_pct"] for r in sport_rows if r.get("edge_pct")]
                if not edges:
                    continue
                tiers = {"HIGH_VALUE": 0, "VALUE": 0, "LOW_VALUE": 0}
                for r in sport_rows:
                    t = r.get("risk_flag") or "LOW_VALUE"
                    if t in tiers:
                        tiers[t] += 1
                audit_data[sport] = {
                    "count":       len(edges),
                    "avg_edge":    round(sum(edges) / len(edges), 2),
                    "best_edge":   round(max(edges), 2),
                    "tiers":       tiers,
                    "recent_edges": edges[:10],
                }

            # ── Merge CLV data if available ───────────────────────────
            for sport in set(r.get("sport", "") for r in clv_rows):
                if not sport:
                    continue
                sv = [r["clv_pct"] for r in clv_rows if r.get("sport") == sport]
                if not sv:
                    continue
                hits = sum(1 for c in sv if c >= 0)
                entry = audit_data.setdefault(sport, {"count": 0, "avg_edge": 0, "best_edge": 0, "tiers": {}, "recent_edges": []})
                entry["clv_count"] = len(sv)
                entry["hit_rate"]  = round(hits / len(sv) * 100, 1)
                entry["avg_clv"]   = round(sum(sv) / len(sv), 2)

            # ── Thresholds (learning layer) ───────────────────────────
            try:
                t_res = sb.table("meta").select("key,value").like("key", "threshold_%").execute()
                for row in (t_res.data or []):
                    sport_key = row["key"].replace("threshold_", "")
                    thresholds[sport_key] = float(row["value"])
            except Exception:
                pass

            # ── Recent signals table ──────────────────────────────────
            recent_signals = active_rows[:20]

            # ── Global KPIs ───────────────────────────────────────────
            if active_rows:
                all_edges = [r["edge_pct"] for r in active_rows if r.get("edge_pct")]
                global_stats["total"]        = len(active_rows)
                global_stats["avg_edge"]     = round(sum(all_edges) / len(all_edges), 2) if all_edges else 0
                global_stats["high_value"]   = sum(1 for r in active_rows if r.get("risk_flag") == "HIGH_VALUE")
                global_stats["value"]        = sum(1 for r in active_rows if r.get("risk_flag") == "VALUE")
                global_stats["low_value"]    = sum(1 for r in active_rows if r.get("risk_flag") == "LOW_VALUE")
                global_stats["sports_count"] = len(audit_data)
                global_stats["has_clv"]      = bool(clv_rows)

    except Exception as e:
        log.error("Audit: %s", e)

    return render_template("audit.html",
                           audit_data=audit_data,
                           thresholds=thresholds,
                           recent_signals=recent_signals,
                           global_stats=global_stats)


# ── JSON API ─────────────────────────────────────────────────────────


@app.route("/performance")
def performance():
    rows: list      = []
    history: list   = []   # sous-ensemble de `rows` affiché dans le tableau HISTORIQUE
    monthly: list   = []
    global_s: dict  = {}
    try:
        sb = _db()
        if sb:
            res = (sb.table("ai_learning_ledger")
                   .select("*")
                   .order("created_at", desc=True)
                   .limit(500)
                   .execute())
            rows = res.data or []

            if rows:
                # Learning layer state — current thresholds and why they
                # last moved (core/learning_layer.py), plus which sports (if
                # any) have their own circuit breaker tripped
                # (core/risk_manager.py). Nested under `if rows` so an empty
                # ledger still falls through to the existing empty-state
                # page below instead of a half-populated `global_s`.
                global_s["thresholds"] = _load_thresholds(sb)
                global_s["segment_thresholds"] = _load_segment_thresholds(sb)
                global_s["learning_summary"] = _load_learning_summary(sb)
                global_s["paused_sports"] = [s for s in _SPORT_DEFAULTS if _is_sport_emission_paused(sb, s)]

                settled = [r for r in rows if r.get("outcome") in ("WIN", "LOSS", "PUSH")]
                decisive = [r for r in rows if r.get("outcome") in ("WIN", "LOSS")]
                # Le tableau HISTORIQUE ne montre que les matchs dont le
                # résultat est connu (demande opérateur) — les stats plus haut
                # continuent de se baser sur `rows` complet, y compris les
                # signaux encore non audités.
                history = settled
                wins    = sum(1 for r in settled if r.get("outcome") == "WIN")
                losses  = sum(1 for r in settled if r.get("outcome") == "LOSS")
                pushes  = sum(1 for r in settled if r.get("outcome") == "PUSH")
                clv_all = [r["clv_final"] for r in rows if r.get("clv_final") is not None]
                edges   = [r["initial_edge"] for r in rows if r.get("initial_edge") is not None]

                # Task 4: never show a win rate without its Wilson 95% CI
                # and the tax-adjusted breakeven probability for the
                # segment's average odds — a bare percentage hides both
                # small-sample noise and whether it's even enough to clear
                # TAX_RATE.
                ci_lo, ci_hi = wilson_ci(wins, len(decisive))
                decisive_odds = [r["odds"] for r in decisive if r.get("odds")]
                avg_odds = sum(decisive_odds) / len(decisive_odds) if decisive_odds else None
                breakeven = p_breakeven(avg_odds, _TAX_RATE) if avg_odds else None

                global_s = {
                    "total":        len(rows),
                    "settled":      len(settled),
                    "wins":         wins,
                    "losses":       losses,
                    "pushes":       pushes,
                    "win_rate":     round(wins / max(wins + losses, 1) * 100, 1),
                    "win_rate_lo":  round(ci_lo * 100, 1),
                    "win_rate_hi":  round(ci_hi * 100, 1),
                    "p_breakeven":  round(breakeven * 100, 1) if breakeven is not None else None,
                    "above_breakeven": (breakeven is not None and ci_lo > breakeven),
                    "avg_clv":      round(sum(clv_all) / len(clv_all), 2) if clv_all else None,
                    "avg_edge":     round(sum(edges) / len(edges), 2) if edges else None,
                    "clv_hit":      round(sum(1 for c in clv_all if c >= 0) / max(len(clv_all), 1) * 100, 1) if clv_all else None,
                }

                # Per-sport win rate + Wilson CI + breakeven
                sport_perf: dict = {}
                for sport in sorted(set(r.get("sport", "") for r in decisive) - {""}):
                    sv = [r for r in decisive if r.get("sport") == sport]
                    sw = sum(1 for r in sv if r["outcome"] == "WIN")
                    slo, shi = wilson_ci(sw, len(sv))
                    sodds = [r["odds"] for r in sv if r.get("odds")]
                    savg  = sum(sodds) / len(sodds) if sodds else None
                    sbreak = p_breakeven(savg, _TAX_RATE) if savg else None
                    sport_perf[sport] = {
                        "n":              len(sv),
                        "win_rate":       round(sw / len(sv) * 100, 1),
                        "win_rate_lo":    round(slo * 100, 1),
                        "win_rate_hi":    round(shi * 100, 1),
                        "p_breakeven":    round(sbreak * 100, 1) if sbreak is not None else None,
                        "above_breakeven": (sbreak is not None and slo > sbreak),
                    }
                global_s["by_sport"] = sport_perf

                # Brier score / reliability by predicted-probability bucket
                # — a win rate alone can't detect miscalibration (e.g. picks
                # tagged "80% confident" that only win 60% of the time).
                predictions = [(r["sharp_prob"], 1 if r["outcome"] == "WIN" else 0)
                              for r in decisive if r.get("sharp_prob") is not None]
                global_s["brier_buckets"] = bucket_predictions(predictions) if predictions else None

                # Monthly breakdown
                months_map: dict = {}
                for r in rows:
                    ca = r.get("created_at") or ""
                    mo = ca[:7]  # "2026-07"
                    if not mo:
                        continue
                    m = months_map.setdefault(mo, {"month": mo, "total": 0, "wins": 0, "losses": 0, "pushes": 0, "expired": 0, "clv_sum": 0.0, "clv_n": 0, "edge_sum": 0.0, "edge_n": 0})
                    m["total"] += 1
                    oc = r.get("outcome")
                    if oc == "WIN":     m["wins"]    += 1
                    elif oc == "LOSS":  m["losses"]  += 1
                    elif oc == "PUSH":  m["pushes"]  += 1
                    else:               m["expired"]  += 1
                    if r.get("clv_final") is not None:
                        m["clv_sum"] += r["clv_final"]; m["clv_n"] += 1
                    if r.get("initial_edge") is not None:
                        m["edge_sum"] += r["initial_edge"]; m["edge_n"] += 1

                for m in months_map.values():
                    denom = m["wins"] + m["losses"]
                    m["win_rate"] = round(m["wins"] / denom * 100, 1) if denom else None
                    m["avg_clv"]  = round(m["clv_sum"]  / m["clv_n"],  2) if m["clv_n"]  else None
                    m["avg_edge"] = round(m["edge_sum"] / m["edge_n"], 2) if m["edge_n"] else None

                monthly = sorted(months_map.values(), key=lambda x: x["month"], reverse=True)

    except Exception as e:
        log.error("Performance: %s", e)

    return render_template("performance.html", rows=rows, history=history, monthly=monthly, global_s=global_s)


@app.route("/system")
def system():
    return render_template("system.html")


# ── JSON API ─────────────────────────────────────────────────────────

@app.route("/api/signals")
def api_signals():
    try:
        sb = _db()
        if not sb:
            return jsonify({"error": "no db"}), 503
        res = sb.table("signals").select("*").eq("status", "active").order("created_at", desc=True).limit(50).execute()
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
    return jsonify({"status": "ok", "version": "8.8", "source": "harvester+ai_search"})


@app.route("/api/odds-quota")
def api_odds_quota():
    # GET /v4/sports is the one Odds API endpoint that does NOT consume
    # quota — it's the documented way to read x-requests-remaining without
    # burning a request, so this can be polled from the dashboard freely.
    api_key = os.environ.get("ODDS_API_KEY")
    if not api_key:
        return jsonify({"error": "ODDS_API_KEY non configurée"}), 503
    try:
        r = requests.get(f"{_ODDS_BASE_URL}/sports", params={"apiKey": api_key}, timeout=10)
        if r.status_code in (401, 403):
            return jsonify({"error": "Clé ODDS_API_KEY invalide ou expirée"}), 502
        if r.status_code != 200:
            return jsonify({"error": f"HTTP {r.status_code}"}), 502
        remaining = r.headers.get("x-requests-remaining")
        used = r.headers.get("x-requests-used")
        return jsonify({
            "remaining": int(remaining) if remaining and remaining.isdigit() else None,
            "used": int(used) if used and used.isdigit() else None,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


_SCAN_REQUEST_COOLDOWN_S = 120  # golden_hour.yml picks this up on its next run (every 30 min)


@app.route("/api/scan", methods=["POST"])
def trigger_scan():
    # This route WRITES to meta (scan_request) — RLS rejects that from the
    # anon key, so it needs the same service_role client the batch scripts
    # use, not the read-only _db() the rest of this file relies on.
    try:
        sb = _db(write=True)
    except MissingCredentialsError as e:
        log.error("scan trigger: %s", e)
        # Surface the specific diagnostic (e.g. "decodes to role='anon'") —
        # it doesn't leak the secret itself, only the JWT's role claim, and
        # it's the difference between "not set" and "set to the wrong key"
        # without needing to go dig through Vercel's function logs.
        return jsonify({"error": f"Écriture Supabase impossible sur ce déploiement : {e}"}), 503
    if not sb:
        return jsonify({"error": "Base de données non configurée"}), 503
    try:
        pending = _get_meta(sb, "scan_request")
        if pending and pending.get("requested_at"):
            try:
                requested_at = datetime.fromisoformat(pending["requested_at"].replace("Z", "+00:00"))
                age_s = (datetime.now(_tz.utc) - requested_at).total_seconds()
            except Exception:
                age_s = None
            if age_s is not None and age_s < _SCAN_REQUEST_COOLDOWN_S:
                return jsonify({
                    "status":  "already_queued",
                    "message": "Un scan est déjà en attente — réessayez dans quelques minutes",
                }), 429

        sb.table("meta").upsert({
            "key":   "scan_request",
            "value": json.dumps({"requested_at": datetime.now(_tz.utc).isoformat()}),
        }, on_conflict="key").execute()
        return jsonify({"status": "queued", "message": "Scan demandé — résultats sous 30 min max (prochain passage planifié)"}), 200
    except Exception as exc:
        log.error("scan queue error: %s", exc)
        return jsonify({"error": str(exc)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
