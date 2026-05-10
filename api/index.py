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

# ═══════════════════════════════════════════════════════════════════
# MODE HUNTER 48h — Full Spectrum (PhD MIT Emergency Protocol)
# Seuil de détection : 1.0% display (saturé), 2.5% elite (qualité)
# ═══════════════════════════════════════════════════════════════════
ALPHA_DISPLAY_MIN = 0.010  # 1.0% — Affiche tout le flux
ALPHA_ELITE_MIN   = 0.025  # 2.5% — Alerte ELITE seulement


def _market_label(market_key: str, sport: str) -> str:
    """Retourne un label lisible pour le type de marché."""
    if not market_key:
        return "N/A"
    if market_key == "h2h":
        if "soccer" in sport:
            return "Draw No Bet"
        return "Moneyline"
    if market_key == "spreads":
        return "Asian Handicap"
    if market_key == "totals":
        return "Over/Under"
    return market_key.upper()


def _get_bookmaker_badge(bm_key: str) -> dict:
    """
    Retourne un badge stylisé pour le bookmaker.
    Permet au dashboard d'afficher visuellement la source de l'Alpha.
    """
    key = bm_key.lower()
    badges = {
        "bet365": {"name": "Bet365", "color": "#1e88e5", "icon": "🏛️"},
        "unibet": {"name": "Unibet", "color": "#43a047", "icon": "🎯"},
        "williamhill": {"name": "William Hill", "color": "#e53935", "icon": "👑"},
        "bwin": {"name": "Bwin", "color": "#fb8c00", "icon": "⚽"},
        "stake": {"name": "Stake", "color": "#8e24aa", "icon": "🎰"},
        "bcgame": {"name": "BC.Game", "color": "#00acc1", "icon": "🎲"},
        "betway": {"name": "Betway", "color": "#1e88e5", "icon": "🎮"},
        "paddypower": {"name": "Paddy Power", "color": "#43a047", "icon": "☘️"},
        "melbet": {"name": "Melbet", "color": "#e53935", "icon": "🔴"},
        "22bet": {"name": "22Bet", "color": "#fb8c00", "icon": "🟠"},
        "linebet": {"name": "Linebet", "color": "#8e24aa", "icon": "🟣"},
        "onexbet": {"name": "1XBet", "color": "#2e7d32", "icon": "🟢"},
        "1xbet": {"name": "1XBet", "color": "#2e7d32", "icon": "🟢"},
    }
    return badges.get(key, {"name": bm_key.capitalize(), "color": "#757575", "icon": "📊"})


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


# ── Debug Events — Diagnostic direct API ──────────────────────

@app.route('/api/debug-events')
def debug_events():
    """
    Appelle l'API The-Odds-API directement et retourne les événements bruts.
    Permet de diagnostiquer pourquoi le scanner trouve 0 signaux.
    """
    try:
        from data.odds_fetcher import OddsFetcher
        fetcher = OddsFetcher()
        events = fetcher.fetch_upcoming_odds()

        summary = []
        for e in events[:30]:  # max 30 pour lisibilité
            bms = [b.get("key") for b in e.get("bookmakers", [])]
            summary.append({
                "match": f"{e.get('home_team')} vs {e.get('away_team')}",
                "sport": e.get("sport_key"),
                "commence": e.get("commence_time"),
                "bookmakers": bms,
                "has_pinnacle": "pinnacle" in bms,
                "has_betfair": "betfair_ex_back" in bms,
                "has_1xbet": any("1x" in b or "onex" in b for b in bms),
                "markets": [
                    {"key": m.get("key"), "outcomes": len(m.get("outcomes", []))}
                    for bm in e.get("bookmakers", []) for m in bm.get("markets", [])
                ][:5],
            })

        return jsonify({
            "total_events": len(events),
            "quota": fetcher.get_quota_status(),
            "events": summary,
        })
    except Exception as e:
        logger.error(f"Erreur debug-events: {e}")
        return jsonify({"error": str(e)}), 500


# ── Screener avec logs de debug ───────────────────────────────

@app.route('/api/screener')
def screener():
    """Déclenche un scan PAIM complet via l'endpoint global upcoming."""
    try:
        from config import settings
        from signals.scanner import MarketScanner
        import asyncio

        # ── Étape 1 : Vider les signaux pending (résidus null + Draw) ──────────
        try:
            db = get_db()
            cleared = db.clear_signals()
            logger.info(f"🗑️ Screener: {cleared} signaux anciens supprimés avant scan")
            # Purger les résidus Draw (violation Binary Synthesis doctrine)
            for draw_word in ("draw", "nul", "match nul"):
                try:
                    db._client.table("signals").delete().ilike("selection", draw_word).execute()
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"clear_signals non critique: {e}")

        # ── Étape 2 : Scan global upcoming (1% seuil display) ──────────
        scanner = MarketScanner(bankroll=settings.starting_bankroll)
        scanner.engine.min_ev_threshold = ALPHA_DISPLAY_MIN  # 1.0%

        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(scanner.run_scan())
        finally:
            loop.close()

        quota_status = scanner.fetcher.get_quota_status()

        # ── Étape 3 : Diagnostic quota ───────────────────────────────
        if quota_status.get("remaining_requests", -1) == 0:
            return jsonify({
                "status": "quota_empty",
                "message": "Quota épuisé ou aucun match trouvé. Attendre le renouvellement mensuel.",
                "signals_found": 0,
                "api_quota": quota_status,
            })

        result_data = {
            "events_analyzed": result.events_analyzed,
            "signals_found": result.signals_found,
            "signals_validated": result.signals_validated,
            "duration_seconds": result.duration_seconds,
        }
        logger.info(f"✅ Screener: {result.signals_validated} signaux | {result.events_analyzed} events")

        # ── Morning Brief Telegram (même si 0 signaux) ──────────────
        import asyncio as _aio
        try:
            from core.notifications import TelegramNotifier
            _notifier = TelegramNotifier()
            if _notifier.enabled:
                async def _brief():
                    n = result.signals_validated
                    icon = "🔥" if n >= 3 else ("✅" if n > 0 else "🌫️")
                    msg = (
                        f"{icon} *PREDATOR PAIM — Scan terminé*\n"
                        f"📊 Événements analysés: `{result.events_analyzed}`\n"
                        f"🎯 Signaux validés: `{n}`\n"
                        f"⏱️ Durée: `{result.duration_seconds}s`\n"
                        f"{'💰 Signaux disponibles sur le dashboard.' if n > 0 else '🌫️ Marché efficient — capital préservé.'}"
                    )
                    await _notifier.bot.send_message(
                        chat_id=_notifier.chat_id, text=msg, parse_mode="Markdown"
                    )
                _loop = _aio.new_event_loop()
                try:
                    _loop.run_until_complete(_brief())
                finally:
                    _loop.close()
        except Exception as _te:
            logger.warning(f"Morning Brief Telegram non critique: {_te}")

        return jsonify({
            "status": "success",
            "result": result_data,
            "threshold_used": ALPHA_DISPLAY_MIN,
            "api_quota": quota_status,
        })

    except Exception as e:
        logger.error(f"Erreur screener: {traceback.format_exc()}")
        return jsonify({"status": "error", "message": str(e)}), 500


# ── Rotation Scan — PhD MIT Protocol (Rate Limiting) ──────────

@app.route('/api/rotation-scan')
def rotation_scan():
    """
    Protocole de Rotation de Scan — Un sport à la fois.
    
    Respecte le Rate Limiting The-Odds-API (15 RPM max).
    Appelé par GitHub Actions toutes les 4-5 minutes.
    Cycle complet: ~70 minutes pour 14 sports.
    
    Returns:
        JSON avec le sport scanné, signaux trouvés, état rotation
    """
    try:
        from core.rotation_engine import execute_rotation_scan
        
        result = execute_rotation_scan()
        return jsonify(result)
        
    except Exception as e:
        logger.error(f"❌ Erreur rotation scan: {traceback.format_exc()}")
        return jsonify({
            "status": "error",
            "message": str(e),
            "protocol": "PhD MIT Rotation Engine v3.6"
        }), 500


@app.route('/api/rotation-status')
def rotation_status():
    """
    Retourne l'état actuel de la rotation.
    
    Returns:
        JSON avec last_index, next_sport, scan_count, watchlist
    """
    try:
        from core.rotation_engine import get_rotation_summary
        
        summary = get_rotation_summary()
        return jsonify({
            "status": "ok",
            "protocol": "PhD MIT Rotation Engine v3.6",
            **summary
        })
        
    except Exception as e:
        logger.error(f"❌ Erreur rotation status: {e}")
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500


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
    """Retourne les 15 derniers signaux pending de Supabase, propres et sans fake data."""
    try:
        db = get_db()
        response = (
            db._client.table("signals")
            .select("*")
            .eq("status", "pending")
            .order("created_at", desc=True)
            .limit(15)
            .execute()
        )
        signals = response.data or []

        _DRAW_WORDS = {"draw", "nul", "match nul"}
        valid = [
            s for s in signals
            if s.get("match_name")
            and s.get("alpha_spread") is not None
            and str(s.get("selection", "")).lower() not in _DRAW_WORDS
        ]

        return jsonify({"signals": valid, "count": len(valid)})
    except Exception as e:
        logger.error(f"Erreur /api/data: {traceback.format_exc()}")
        return jsonify({"signals": [], "count": 0, "error": str(e)})


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
    capital = 10000 # Use the same capital value as in get_stats
    try:
        db = get_db()
        # Fetch signals with status 'bet_placed' or 'active'
        active_signals_response = db._client.table("signals").select("recommended_stake").in_("status", ["bet_placed", "active"]).execute()
        active_signals = active_signals_response.data or []

        total_exposure = sum(s.get("recommended_stake", 0) for s in active_signals)

        if total_exposure == 0:
            risk_cluster_status = "SÉCURISÉ"
            risk_percentage = 0.0
        else:
            risk_percentage = (total_exposure / capital) * 100
            # Define thresholds for CRITICAL, ÉLEVÉ, MODÉRÉ, FAIBLE
            if risk_percentage > 25:
                risk_cluster_status = "CRITIQUE"
            elif risk_percentage > 10:
                risk_cluster_status = "ÉLEVÉ"
            elif risk_percentage > 2:
                risk_cluster_status = "MODÉRÉ"
            else:
                risk_cluster_status = "FAIBLE"

        return jsonify({"risk_cluster": risk_cluster_status, "total_exposure": total_exposure, "risk_percentage": round(risk_percentage, 2)})
    except Exception as e:
        print(f"DEBUG EXPOSURE ERROR: {traceback.format_exc()}")
        return jsonify({"risk_cluster": "ERREUR", "total_exposure": 0, "risk_percentage": 0.0})


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


@app.route('/api/test-telegram')
def test_telegram():
    """Teste la connectivité Telegram et envoie un message de diagnostic."""
    import asyncio
    try:
        from core.notifications import TelegramNotifier
        notifier = TelegramNotifier()
        if not notifier.enabled:
            return jsonify({"status": "disabled", "message": "Telegram non configuré (token ou chat_id manquant)"})

        async def _send():
            await notifier.bot.send_message(
                chat_id=notifier.chat_id,
                text="🦅 *PREDATOR PAIM* — Test Telegram OK ✅\n_Système opérationnel._",
                parse_mode="Markdown",
            )

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_send())
        finally:
            loop.close()

        return jsonify({"status": "sent", "message": "Message Telegram envoyé avec succès"})
    except Exception as e:
        logger.error(f"Erreur test-telegram: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


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


# ═══════════════════════════════════════════════════════════════════
# ALPHA DECAY TRACKER (PAIM v5.0 Perfection)
# ═══════════════════════════════════════════════════════════════════

@app.route('/api/alpha-decay/<signal_id>')
def get_alpha_decay(signal_id: str):
    """
    Retourne les métriques de dégradation Alpha pour un signal.
    Urgency status: 🟢 STABLE | 🟡 MODÉRÉ | 🔥 CRITIQUE
    """
    try:
        from core.alpha_decay import get_decay_tracker
        
        tracker = get_decay_tracker()
        metrics = tracker.get_decay_metrics(signal_id)
        
        if not metrics:
            return jsonify({
                "signal_id": signal_id,
                "status": "insufficient_data",
                "message": "Pas assez d'historique (min 2 snapshots requis)"
            })
        
        return jsonify({
            "signal_id": metrics.signal_id,
            "current_ev_pct": round(metrics.current_ev * 100, 2),
            "initial_ev_pct": round(metrics.initial_ev * 100, 2),
            "decay_rate_per_hour": metrics.decay_rate,
            "half_life_minutes": metrics.half_life_minutes,
            "urgency_status": metrics.urgency_status,
            "stability_score": metrics.stability_score,
            "snapshots_count": metrics.snapshots_count,
            "action_recommendation": metrics.action_recommendation,
            "minutes_until_expiry": metrics.minutes_until_expiry,
        })
    except Exception as e:
        logger.error(f"Erreur Alpha Decay: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/risk-cluster')
def get_risk_cluster():
    """
    Analyse la corrélation de portefeuille et retourne les warnings.
    Règles: Max 20% capital/sport, Max 10% capital/ligue
    """
    try:
        from collections import defaultdict
        
        db = get_db()
        
        # Récupérer signaux pending
        response = db._client.table("signals")\
            .select("sport, alpha_spread, recommended_stake")\
            .eq("status", "pending")\
            .execute()
        
        signals = response.data or []
        
        if not signals:
            return jsonify({
                "status": "ok",
                "total_exposure": 0,
                "warnings": [],
                "clusters": {}
            })
        
        # Analyse par sport
        sport_exposure = defaultdict(lambda: {"count": 0, "stake": 0, "alpha": 0})
        
        for sig in signals:
            sport = sig.get("sport", "unknown")
            stake = sig.get("recommended_stake", 0) or 0
            alpha = sig.get("alpha_spread", 0) or 0
            
            sport_exposure[sport]["count"] += 1
            sport_exposure[sport]["stake"] += stake
            sport_exposure[sport]["alpha"] += alpha
        
        # Vérifier seuils
        from config import settings
        total_bankroll = settings.starting_bankroll
        
        warnings = []
        clusters = {}
        
        for sport, data in sport_exposure.items():
            stake_pct = data["stake"] / total_bankroll
            count = data["count"]
            
            clusters[sport] = {
                "signal_count": count,
                "total_stake": round(data["stake"], 2),
                "avg_alpha": round(data["alpha"] / count * 100, 2) if count > 0 else 0,
                "capital_pct": round(stake_pct * 100, 2)
            }
            
            # Règles de diversification
            if stake_pct > 0.20:
                warnings.append({
                    "level": "critical",
                    "sport": sport,
                    "message": f"🔥 {sport}: {stake_pct:.1%} capital exposé (max 20%)",
                    "action": "DIVERSIFIER IMMÉDIATEMENT"
                })
            elif count > 3:
                warnings.append({
                    "level": "warning",
                    "sport": sport,
                    "message": f"⚠️ {sport}: {count} signaux (risque cluster)",
                    "action": "SURVEILLER CORRÉLATION"
                })
        
        total_exposure = sum(d["stake"] for d in sport_exposure.values())
        
        return jsonify({
            "status": "warning" if warnings else "ok",
            "total_exposure": round(total_exposure, 2),
            "exposure_pct": round(total_exposure / total_bankroll * 100, 2),
            "warnings": warnings,
            "clusters": clusters,
            "max_recommended": round(total_bankroll * 0.20, 2),
        })
        
    except Exception as e:
        logger.error(f"Erreur Risk Cluster: {e}")
        return jsonify({"error": str(e)}), 500


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


# ═══════════════════════════════════════════════════════════════════
# MODE HUNTER 48h — Full Spectrum Multi-Sport (Emergency Protocol)
# ═══════════════════════════════════════════════════════════════════

# Sports prioritaires pour le mode Hunter (saturation)
HUNTER_SPORTS = [
    'basketball_nba',           # 🏀 Playoffs - Priorité Alpha MAX
    'tennis_atp_masters_1000',  # 🎾 Rome/Madrid
    'soccer_epl',               # ⚽ Matchs fin de saison
    'soccer_uefa_champs_league', # ⚽ UCL
    'soccer_spain_la_liga',     # ⚽ La Liga
    'baseball_mlb',             # ⚾ MLB très prévisible
    'esports_lol_msi',          # 🎮 MSI LoL
]


@app.route('/api/hunter-scan', methods=['POST'])
def hunter_scan():
    """
    MODE HUNTER 48h — Débridage total du moteur PAIM.
    
    Actions:
    1. 🗑️ Purge tous les anciens signaux de Supabase
    2. 🎯 Scan saturation des 7 sports majeurs (48h window)
    3. ⚡ Shin Method sur chaque sport
    4. 📊 Seuil 1.0% (affichage), 2.5% (ELITE)
    
    Returns:
        JSON avec résultat du scan et compte de signaux
    """
    try:
        import asyncio
        from signals.scanner import MarketScanner
        from config import settings
        
        logger.info("🚨 MODE HUNTER ACTIVÉ — Saturation Multi-Sport 48h")
        
        # ── ÉTAPE 1: PURGE DES ANCIENS SIGNAUX ─────────────────────────
        try:
            db = get_db()
            # Supprime tous les signaux pending (pas les settled/history)
            result = db._client.table("signals").delete().eq("status", "pending").execute()
            logger.info(f"🗑️ Purge: {len(result.data or [])} anciens signaux supprimés")
        except Exception as e:
            logger.warning(f"⚠️ Purge partielle: {e}")
        
        # ── ÉTAPE 2: SCAN SATURATION MULTI-SPORT ──────────────────────
        scanner = MarketScanner(bankroll=settings.starting_bankroll)
        scanner.engine.min_ev_threshold = ALPHA_DISPLAY_MIN  # 1.0%
        
        all_signals = []
        scan_results = []
        
        for sport in HUNTER_SPORTS:
            try:
                logger.info(f"🎯 Chasse sur {sport}...")
                
                loop = asyncio.new_event_loop()
                result = loop.run_until_complete(scanner.run_single_sport_scan(sport))
                loop.close()
                
                # 🔧 CORRECTION: Accumuler les signaux trouvés
                all_signals.extend(result.signals)
                
                scan_results.append({
                    "sport": sport,
                    "events": result.events_analyzed,
                    "signals": result.signals_validated,
                    "signals_found": len(result.signals)
                })
                
                logger.info(f"✅ {sport}: {result.signals_validated} signaux validés / {len(result.signals)} trouvés")
                
            except Exception as e:
                logger.error(f"❌ Erreur {sport}: {e}")
                scan_results.append({"sport": sport, "error": str(e)})
        
        # ── ÉTAPE 3: RÉSUMÉ ─────────────────────────────────────────
        total_signals = len(all_signals)
        elite_signals = sum(1 for s in all_signals if (s.alpha_spread or 0) >= ALPHA_ELITE_MIN)
        
        logger.info(f"🎯 HUNTER COMPLET: {total_signals} signaux trouvés ({elite_signals} ELITE)")
        
        return jsonify({
            "status": "success",
            "mode": "HUNTER_48H",
            "protocol": "PhD MIT Emergency v4.0",
            "sports_scanned": len(HUNTER_SPORTS),
            "total_signals": total_signals,
            "elite_signals": elite_signals,
            "threshold_display": f"{ALPHA_DISPLAY_MIN*100:.1f}%",
            "threshold_elite": f"{ALPHA_ELITE_MIN*100:.1f}%",
            "scan_details": scan_results,
            "window": "48h",
            "next_action": "Attendre 5-10 min puis rafraîchir /api/data"
        })
        
    except Exception as e:
        logger.error(f"Erreur debug: {e}")
        return jsonify({"error": str(e)}), 500


# ═══════════════════════════════════════════════════════════════════
# GEMINI ORACLE ENDPOINT — Test direct du fallback AI
# ═══════════════════════════════════════════════════════════════════

@app.route('/api/test-gemini-oracle')
def test_gemini_oracle():
    """
    Test direct du Gemini Oracle pour recherche de cotes.
    Ne consomme pas de quota The-Odds-API.
    """
    try:
        from core.gemini_search import GeminiOracle
        
        home = request.args.get('home', 'Manchester City')
        away = request.args.get('away', 'Real Madrid')
        sport = request.args.get('sport', 'soccer')
        
        oracle = GeminiOracle()
        result = oracle.search_odds(home, away, sport)
        
        if not result:
            return jsonify({
                "status": "no_data",
                "message": "Gemini n'a pas trouvé de cotes",
                "match": f"{home} vs {away}"
            })
        
        return jsonify({
            "status": "success",
            "match": f"{result.home_team} vs {result.away_team}",
            "pinnacle": {
                "home": result.pinnacle_home,
                "away": result.pinnacle_away
            },
            "onexbet": {
                "home": result.onexbet_home,
                "away": result.onexbet_away
            },
            "match_time": result.match_time,
            "confidence": result.confidence,
            "source": result.source
        })
        
    except Exception as e:
        logger.error(f"Erreur test Gemini Oracle: {e}")
        return jsonify({"status": "error", "error": str(e)}), 500


# ═══════════════════════════════════════════════════════════════════
# DIAGNOSTIC — Test API The-Odds-API en direct
# ═══════════════════════════════════════════════════════════════════

@app.route('/api/test-odds-api')
def test_odds_api():
    """
    Test direct de l'API The-Odds-API pour voir si des données sont disponibles.
    Retourne les matchs bruts trouvés pour basketball_nba (sport test).
    """
    try:
        import requests
        from config import settings
        
        sport = request.args.get('sport', 'basketball_nba')
        
        url = f"https://api.the-odds-api.com/v4/sports/{sport}/odds"
        params = {
            "apiKey": settings.odds_api_key,
            "regions": "eu",
            "markets": "h2h",
            "oddsFormat": "decimal",
            "dateFormat": "iso",
        }
        
        logger.info(f"🔍 Test API direct: {sport}")
        
        response = requests.get(url, params=params, timeout=15)
        
        if response.status_code != 200:
            return jsonify({
                "status": "error",
                "api_status": response.status_code,
                "message": f"API erreur {response.status_code}",
                "response": response.text[:500]
            }), 500
        
        events = response.json()
        
        # Filtrer les événements dans les 48h côté Python
        from datetime import datetime, timedelta
        now = datetime.now()
        cutoff = now + timedelta(hours=48)
        
        filtered_events = []
        for evt in events:
            commence_time = evt.get("commence_time", "")
            if commence_time:
                try:
                    evt_time = datetime.fromisoformat(commence_time.replace('Z', '+00:00'))
                    if now <= evt_time <= cutoff:
                        filtered_events.append(evt)
                except:
                    filtered_events.append(evt)  # Si erreur parsing, on garde
        
        if not filtered_events:
            return jsonify({
                "status": "no_data",
                "sport": sport,
                "message": "Aucun événement trouvé dans les 48h",
                "total_raw": len(events),
                "api_quota_remaining": response.headers.get("x-requests-remaining", "?"),
            })
        
        # Analyser les données reçues
        events_with_sharp = 0
        events_with_soft = 0
        sample_events = []
        
        for evt in filtered_events[:5]:  # Premier 5 pour l'exemple
            bookmakers = evt.get("bookmakers", [])
            has_pinnacle = any("pinnacle" in bm.get("key", "").lower() for bm in bookmakers)
            has_soft = any(any(s in bm.get("key", "").lower() for s in ["1xbet", "onexbet", "bet365"]) for bm in bookmakers)
            
            if has_pinnacle:
                events_with_sharp += 1
            if has_soft:
                events_with_soft += 1
            
            sample_events.append({
                "match": f"{evt.get('home_team')} vs {evt.get('away_team')}",
                "time": evt.get("commence_time"),
                "bookmakers_count": len(bookmakers),
                "has_pinnacle": has_pinnacle,
                "has_soft": has_soft,
                "bookies": [bm.get("key") for bm in bookmakers[:3]]
            })
        
        return jsonify({
            "status": "success",
            "sport": sport,
            "total_events_48h": len(filtered_events),
            "total_raw": len(events),
            "events_with_pinnacle": events_with_sharp,
            "events_with_soft": events_with_soft,
            "api_quota_remaining": response.headers.get("x-requests-remaining", "?"),
            "api_quota_used": response.headers.get("x-requests-used", "?"),
            "sample_events": sample_events,
            "message": f"✅ {len(filtered_events)} événements 48h, {events_with_sharp} avec Pinnacle, {events_with_soft} avec soft"
        })
        
    except Exception as e:
        logger.error(f"Erreur test API: {e}")
        return jsonify({
            "status": "error",
            "error": str(e),
            "trace": str(e.__traceback__)
        }), 500


# ═══════════════════════════════════════════════════════════════════
# DOCTRINE BINARY SYNTHESIS — Outils de Maintenance
# ═══════════════════════════════════════════════════════════════════

@app.route('/api/purge-draw-signals', methods=['POST'])
def purge_draw_signals():
    """
    Nettoie la base de données de tous les signaux contenant 'Draw'.
    Doctrine Zéro Nul: le match nul est strictement interdit.
    
    Returns:
        JSON avec nombre de signaux supprimés
    """
    try:
        db = get_db()
        
        # Récupérer tous les signaux pending
        response = db._client.table("signals").select("id, selection, match_name").eq("status", "pending").execute()
        signals = response.data or []
        
        # Filtrer ceux avec Draw
        draw_signals = [s for s in signals if s.get("selection", "").lower().startswith("draw")]
        
        deleted_count = 0
        for sig in draw_signals:
            try:
                db._client.table("signals").delete().eq("id", sig["id"]).execute()
                deleted_count += 1
                logger.info(f"🗑️ Supprimé signal Draw: {sig.get('match_name')} | {sig.get('selection')}")
            except Exception as e:
                logger.error(f"❌ Erreur suppression {sig.get('id')}: {e}")
        
        return jsonify({
            "status": "success",
            "doctrine": "BINARY SYNTHESIS v4.1",
            "action": "PURGE_DRAW_SIGNALS",
            "scanned": len(signals),
            "deleted": deleted_count,
            "message": f"🚫 {deleted_count} signaux 'Draw' supprimés. Base nettoyée."
        })
        
    except Exception as e:
        logger.error(f"❌ Erreur purge: {e}")
        return jsonify({
            "status": "error",
            "error": str(e)
        }), 500


@app.route('/api/binary-audit')
def binary_audit():
    """
    Audit de conformité binaire — vérifie qu'aucun signal Draw n'existe.
    
    Returns:
        JSON avec statut de conformité
    """
    try:
        db = get_db()
        
        # Vérifier les signaux pending
        response = db._client.table("signals").select("id, selection, match_name, sport").eq("status", "pending").execute()
        signals = response.data or []
        
        violations = []
        for sig in signals:
            sel = sig.get("selection", "").lower()
            if any(forbidden in sel for forbidden in ["draw", "nul", "match nul"]):
                violations.append({
                    "id": sig.get("id"),
                    "match": sig.get("match_name"),
                    "selection": sig.get("selection"),
                    "sport": sig.get("sport")
                })
        
        return jsonify({
            "status": "ok",
            "doctrine": "BINARY SYNTHESIS v4.1",
            "compliant": len(violations) == 0,
            "total_signals": len(signals),
            "violations_count": len(violations),
            "violations": violations,
            "message": "✅ Conforme binaire" if len(violations) == 0 else f"🚫 {len(violations)} violations détectées"
        })
        
    except Exception as e:
        logger.error(f"❌ Erreur audit: {e}")
        return jsonify({
            "status": "error",
            "error": str(e)
        }), 500