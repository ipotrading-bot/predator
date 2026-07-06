"""
run_engine.py — PREDATOR PAIM v8.8 — Hunter Multi-Sport + Portfolio Balancer
Markets: h2h (NBA/Tennis) | spreads (NBA/Soccer) | totals (all)
Sharp filter: Prob. Sharp (Shin devigged) >= threshold per market type
Pipeline: OddsAPI → Gemini Search → Gemini Estimator → AH0.0/ML/PS/OU → Edge → Balancer → Supabase
All timestamps : UTC/GMT — no local-time contamination.
"""
import json
import logging
import os
import random
import time
import signal
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv
from supabase import create_client

from core.harvester import fetch_matches, fetch_pinnacle_prices, fetch_estimated_prices, fetch_mma_events, fetch_esports_events, fetch_alternative_sports_batch, fetch_betfair_prices
from core.math_engine import to_binary, devig_prob
from core.odds_api import fetch_odds
from core.oracle import get_pinnacle_price
from core.learning_layer import load_thresholds as _load_thresholds
from core.paim_engine import (
    compute_alpha, MIN_EDGE, strict_team_match,
    market_label, SHARP_PROB_BY_MARKET, calculate_consensus_price,
)
from core.constants import ELITE_EDGE as _ELITE_EDGE, SOCCER_ELITE_EDGE as _SOCCER_ELITE_EDGE, BASKETBALL_ELITE_EDGE as _BASKETBALL_ELITE_EDGE, kelly_stake as _kelly_stake, risk_flag as _risk_flag, SUSPECT_EDGE as _SUSPECT_EDGE, KELLY_FRACTION as _KELLY_FRACTION, AH0_VALUE_THRESHOLD as _AH0_VALUE_THRESHOLD, PURGE_EDGE_FLOOR as _PURGE_EDGE_FLOOR, MLB_LINEUP_WINDOW_H as _MLB_LINEUP_WINDOW_H, PUSH_PROB_ROUND_LINE as _PUSH_PROB_ROUND_LINE

load_dotenv()

# ── Deep-scan mode (DEEP_SCAN=1) ─────────────────────────────────────
# Triggered by .github/workflows/deep_scan.yml or manually.
# Lifts per-sport quotas + scans 48h ahead with up to 100 events.
DEEP_SCAN    = os.environ.get("DEEP_SCAN",    "0") == "1"
GOLDEN_HOUR  = os.environ.get("GOLDEN_HOUR", "0") == "1"
GUERRILLA    = os.environ.get("GUERRILLA",   "0") == "1"  # skip OddsAPI → Tier 2 direct
DEBUG_MODE   = os.environ.get("PREDATOR_DEBUG", "0") == "1"

# ── Global Timeout Handler (Safety Net) ──────────────────────────────
# Prevents Engine from hanging GitHub Actions (5+ min on Tier 2/3 fallback)
from core.constants import GLOBAL_TIMEOUT

def _timeout_handler(signum, frame):
    log.error("TIMEOUT: Engine exceeded %d seconds — exiting gracefully", GLOBAL_TIMEOUT)
    raise TimeoutError(f"Global timeout ({GLOBAL_TIMEOUT}s) exceeded")

signal.signal(signal.SIGALRM, _timeout_handler)
signal.alarm(GLOBAL_TIMEOUT)

# ── UTC logger ────────────────────────────────────────────────────────
_fmt = logging.Formatter(
    fmt="%(asctime)s UTC | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
_fmt.converter = time.gmtime          # Force UTC — ignore server local time
_handler = logging.StreamHandler()
_handler.setFormatter(_fmt)
log = logging.getLogger("PREDATOR")
log.setLevel(logging.DEBUG if DEBUG_MODE else logging.INFO)
log.addHandler(_handler)
log.propagate = False

if DEBUG_MODE:
    log.debug("DEBUG MODE ENABLED — verbose logging active")

SUPABASE_URL   = os.environ.get("SUPABASE_URL")
SUPABASE_KEY   = os.environ.get("SUPABASE_KEY")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT  = os.environ.get("TELEGRAM_CHAT_ID")

ELITE_EDGE  = _ELITE_EDGE   # % — send Telegram alert (from core.constants)
_MAJOR_SPORTS = {"soccer", "basketball", "hockey", "baseball", "rugbyleague", "aussierules"}

# Fast mode (default): 20 events, tight quota — speed over coverage
# Deep mode (DEEP_SCAN=1): 100 events, wide quota — 48h full slate
MAX_MATCHES = 100 if DEEP_SCAN else 50

SPORT_EMOJI  = {
    "soccer": "⚽", "tennis": "🎾", "basketball": "🏀", "boxing": "🥊",
    "mma": "🥋", "darts": "🎯", "cricket": "🏏", "hockey": "🏒",
    "esports": "🎮", "americanfootball": "🏈", "baseball": "⚾",
    "rugby": "🏉", "rugbyleague": "🏉", "aussierules": "🦘",
    "volleyball": "🏐", "tabletennis": "🏓", "handball": "🤾",
}

# Golden Hour — T-120min — 7 sports à lag maximal et volume élevé (juin 2026)
# Budget : 72 exec/j (*/20) × 7 sports = 504 req/j max | ~15 120 req/mois.
# Sélection : Pinnacle+1XBet confirmés + mouvement de ligne pré-match élevé.
#
# WC 2026 (début 11/06) : 3-4 matchs/jour → fenêtres 2h très actives
# KBO/NPB : 09:00–13:00 UTC → lag Asie sur books EU = prime pour Predator
# Copa Lib : 21:00–23:00 UTC → SA evening, lag 1XBet bien documenté
# NBA/NHL Finals : 22:00–02:00 UTC → tip-off windows, mouvement max
GOLDEN_SPORT_KEYS = {
    "soccer_fifa_world_cup":                "soccer",      # ← PRIORITÉ 1 — WC 2026
    "soccer_international_friendlies":      "soccer",      # Amicaux équipes nationales — juin 2026
    "soccer_club_friendly":                 "soccer",      # Amicaux clubs — pré-saison juin–juillet
    "soccer_conmebol_copa_libertadores":    "soccer",      # Copa Lib — lag SA maximal
    "soccer_brazil_campeonato":             "soccer",      # Brasileirão — quotidien
    "soccer_usa_mls":                       "soccer",      # MLS — actif juin–août
    "soccer_argentina_primera_division":    "soccer",      # Liga Argentina — marché SA sharp
    "soccer_mexico_ligamx":                 "soccer",      # Liga MX — actif été
    "basketball_nba":                       "basketball",  # NBA Finals
    "icehockey_nhl":                        "hockey",      # NHL Cup Finals
    "baseball_mlb":                         "baseball",    # MLB — 10+ matchs/jour
    "baseball_kbo":                         "baseball",    # KBO Corée — lag Asie ✓
    "baseball_npb":                         "baseball",    # NPB Japon — lag Asie ✓
    "aussierules_afl":                      "aussierules", # AFL — fenêtre AU morning
    "rugby_nrl":                            "rugbyleague", # NRL — fenêtre AU evening
}

# Portfolio Balancer — quotas max par sport par scan (11 sports actifs uniquement)
# Baseball élevé : MLB+KBO+NPB = 3 ligues simultanées (~19 events/fetch)
# Soccer élevé  : FIFA WC 2026 + Copa Lib + Brasileirão + MLS
_QUOTA_FAST = {
    "soccer":      20,   # WC(5)+Amicaux(5)+Club friendly(4)+Copa Lib(2)+Brasileirão(2)+MLS(2)
    "baseball":    10,   # MLB(5) + KBO(3) + NPB(2)
    "basketball":   8,   # NBA Finals
    "hockey":       6,   # NHL Cup Finals
    "rugbyleague":  5,   # NRL
    "aussierules":  5,   # AFL
}
_QUOTA_DEEP = {
    "soccer":      30,
    "baseball":    16,
    "basketball":  12,
    "hockey":       8,
    "rugbyleague":  8,
    "aussierules":  8,
}
SPORT_QUOTA = _QUOTA_DEEP if DEEP_SCAN else _QUOTA_FAST
# Telegram report order — sports les plus générateurs de signaux en tête
_SPORT_ORDER = ["soccer", "basketball", "hockey", "baseball", "rugbyleague", "aussierules"]

# Sessions marché (UTC) — alignées sur les fenêtres d'inefficience
_SESSIONS = {
    (6,  12): "EU-OPEN  📈",   # 06:00–11:59 — KBO/NPB morning + lignes EU fraîches
    (12, 18): "EU-MID   ⚡",   # 12:00–17:59 — SA + WC afternoon + MLB opening
    (18, 22): "EU-CLOSE 🎯",   # 18:00–21:59 — Copa/WC soirée + NBA/NHL pré-match
}

def _market_session(hour_utc: int) -> str:
    for (start, end), label in _SESSIONS.items():
        if start <= hour_utc < end:
            return label
    return "OVERNIGHT 🌙"      # 22:00–05:59 UTC — NBA/NHL tip-off + MLB late


# ── helpers ──────────────────────────────────────────────────────────

def _telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT, "text": text, "parse_mode": "Markdown"}, timeout=10)
    except Exception as e:
        log.error("Telegram: %s", e)


# Columns optional at DB level — strip only as last-resort fallback
_OPTIONAL_COLS = {"selection_name", "kelly_pct", "advice", "sharp_sources", "consensus_score"}


def _save(sb, signal) -> bool:
    """Delete-then-insert to avoid duplicates. Returns True on success. IMPROVED ERROR HANDLING."""
    from core.constants import MAX_DB_RETRIES, DELAY_DB_RETRY
    
    payload = dict(signal)
    mid  = payload.get("match_id", "")
    mkey = payload.get("market_key", "")
    sig_label = f"{payload.get('match', '?')}/{payload.get('market', '?')}"
    
    for attempt in range(1, MAX_DB_RETRIES + 1):
        try:
            # Delete old version
            if mid and mkey:
                sb.table("signals").delete().eq("match_id", mid).eq("market_key", mkey).execute()
            else:
                sb.table("signals").delete().eq("match", payload["match"]).eq("market", payload.get("market", "")).execute()
        except Exception as e:
            if DEBUG_MODE:
                log.debug("Supabase delete (signal %s): %s", sig_label, str(e)[:80])
        
        # Insert new signal
        try:
            sb.table("signals").insert(payload).execute()
            if DEBUG_MODE:
                log.debug("✓ Signal saved: %s [edge=%.2f%%]", sig_label, payload.get("edge_pct", 0))
            return True
        except Exception as e:
            err = str(e)
            
            # Retry on transient DB errors
            if "FATAL" in err or "connection" in err.lower() or "timeout" in err.lower():
                if attempt < MAX_DB_RETRIES:
                    log.warning("Supabase transient error (signal %s, attempt %d/%d): %s", 
                               sig_label, attempt, MAX_DB_RETRIES, err[:60])
                    time.sleep(DELAY_DB_RETRY)
                    continue
            
            # Graceful fallback: strip optional columns if schema mismatch
            if "does not exist" in err or "column" in err.lower():
                core = {k: v for k, v in payload.items() if k not in _OPTIONAL_COLS}
                try:
                    sb.table("signals").insert(core).execute()
                    log.warning("Signal saved (schema fallback): %s", sig_label)
                    return True
                except Exception as e2:
                    log.error("Supabase insert FAILED after retry (signal %s): %s", sig_label, str(e2)[:80])
            else:
                log.error("Supabase insert FAILED (signal %s): %s", sig_label, err[:80])
            
            return False
    
    log.error("Supabase insert FAILED after %d retries (signal %s)", MAX_DB_RETRIES, sig_label)
    return False


def _get_cached(sb, key: str, ttl_hours: float):
    """Return cached list from Supabase meta if within TTL, else None."""
    try:
        row = sb.table("meta").select("value, updated_at").eq("key", key).maybe_single().execute()
        if not row.data:
            return None
        updated = datetime.fromisoformat(row.data["updated_at"])
        age_h = (datetime.now(timezone.utc) - updated).total_seconds() / 3600
        if age_h < ttl_hours:
            data = json.loads(row.data["value"])
            log.info("Cache HIT [%s] — age %.1fh < %.0fh TTL (%d items)", key, age_h, ttl_hours, len(data))
            return data
    except Exception as e:
        log.debug("Cache get [%s]: %s", key, e)
    return None


def _set_cached(sb, key: str, value: list):
    """Store list in Supabase meta table."""
    try:
        sb.table("meta").upsert({
            "key":        key,
            "value":      json.dumps(value),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }, on_conflict="key").execute()
        log.debug("Cache SET [%s] — %d items", key, len(value))
    except Exception as e:
        log.warning("Cache set [%s]: %s", key, e)


def _heartbeat(sb, scan_time: datetime, matches: int, signals: int):
    try:
        sb.table("meta").upsert({
            "key":        "last_scan",
            "value":      json.dumps({
                "at":      scan_time.isoformat(),
                "matches": matches,
                "signals": signals,
            }),
            "updated_at": scan_time.isoformat(),
        }, on_conflict="key").execute()
        log.info("Heartbeat: last_scan updated (%d matchs, %d signaux)", matches, signals)
    except Exception as e:
        log.error("Supabase heartbeat: %s", e)


def _archive_before_purge(sb, signals: list):
    """Archive active signals to ai_learning_ledger before purging (proxy CLV, outcome=expired)."""
    if not signals:
        return
    archived = 0
    for sig in signals:
        orig_pin = sig.get("pinnacle_price") or 0.0
        clv = round((sig["xbet_odd"] / orig_pin - 1) * 100, 2) if orig_pin > 1.01 else 0.0
        match_time = sig.get("match_time")
        scanned_at = sig.get("scanned_at")
        ttm = None
        if match_time and scanned_at:
            try:
                mt = datetime.fromisoformat(match_time.replace("Z", "+00:00"))
                sc = datetime.fromisoformat(scanned_at.replace("Z", "+00:00"))
                ttm = int((mt - sc).total_seconds() / 60)
            except (ValueError, OverflowError):
                pass
        try:
            sb.table("ai_learning_ledger").insert({
                "match":                 sig["match"],
                "sport":                 sig.get("sport"),
                "league":                sig.get("league"),
                "market_type":           sig.get("market_key"),
                "time_to_match_minutes": ttm,
                "initial_edge":          sig.get("edge_pct"),
                "clv_final":             float(clv),
                "was_clv_positive":      clv > 0,
                "outcome":               "expired",
            }).execute()
            archived += 1
        except Exception as e:
            log.debug("archive_before_purge [%s]: %s", sig.get("match", "?"), str(e)[:60])
    if archived:
        log.info("Archived %d/%d signals to ledger before purge", archived, len(signals))


def _purge_old_signals(sb):
    """Delete stale signals. IMPROVED: batched operations + better logging."""
    from core.paim_engine import MIN_EDGE

    now_iso = datetime.now(timezone.utc).isoformat()
    cutoff_48h = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()

    # ── Archive active signals >48h before purging (preserve ledger history) ──
    try:
        stale = (sb.table("signals")
                 .select("*")
                 .eq("status", "active")
                 .lt("created_at", cutoff_48h)
                 .execute())
        _archive_before_purge(sb, stale.data or [])
    except Exception as e:
        log.debug("fetch stale for archive: %s", e)

    # ── Past-match purge — scoped to status=active ONLY ────────────────
    # BUGFIX: this used to be `.lt("match_time", now_iso)` with no status
    # filter, which deleted settled/closed/expired signals the instant
    # their match kicked off — almost always BEFORE run_audit.py (6h cadence)
    # ever got a chance to settle them and log them to ai_learning_ledger.
    # That silently starved the Ledger/Audit pages and the AI learning layer
    # (compute_and_save) of data. Scoping to status=active preserves terminal
    # signals for audit_engine while still purging genuinely stale active ones.
    try:
        sb.table("signals").delete().eq("status", "active").lt("match_time", now_iso).execute()
    except Exception as e:
        log.debug("Supabase purge (past matches, active only): %s", str(e)[:60])

    # ── Purge Rules (more efficient than 13 individual calls) ──────────
    # active_only=True: these are data-quality gates meant to discard bad
    # ACTIVE candidates before betting — NOT to be applied to settled/closed/
    # expired rows, which must survive for the 48h /ledger + /audit window
    # (see the status='active' scoping rationale above). A settled signal
    # can legitimately have edge_pct > 10 (MAX_EDGE caps it at 15, not 10 —
    # 10-15% is merely a pre-bet SUSPECT_DATA trigger, not a data error once
    # the outcome is known) and must not be deleted for that reason alone.
    purge_rules = [
        ("eq",  "status", "pending",   "status=pending",                        False),
        ("lt",  "created_at", cutoff_48h, ">48h old",                           False),
        ("gt",  "edge_pct", 15.0,                                        "edge > 15% (hard cap)",           True),
        ("gt",  "edge_pct", 10.0,                                        "edge > 10% (SUSPECT)",            True),
        ("lte", "edge_pct", _PURGE_EDGE_FLOOR,                          f"edge <= {_PURGE_EDGE_FLOOR}% (bruit)", True),
        ("lte", "sharp_prob", 0.0,                                       "sharp_prob <= 0",                 True),
        ("is_", "market", "null",                                        "null market",                      True),
        ("is_", "sharp_prob", "null",                                    "sharp_prob=null",                 True),
        ("eq",  "risk_flag", "SUSPECT_DATA",                            "SUSPECT_DATA",                     True),
        ("lte", "xbet_odd", 1.01,                                       "xbet_odd <= 1.01",                True),
        ("lte", "pinnacle_price", 1.01,                                 "pinnacle_price <= 1.01",           True),
    ]

    for op_type, field, value, label, active_only in purge_rules:
        try:
            query = sb.table("signals").delete()
            if active_only:
                query = query.eq("status", "active")
            if op_type == "eq":
                query = query.eq(field, value)
            elif op_type == "lt":
                query = query.lt(field, value)
            elif op_type == "gt":
                query = query.gt(field, value)
            elif op_type == "lte":
                query = query.lte(field, value)
            elif op_type == "is_":
                query = query.is_(field, value)
            query.execute()

            if DEBUG_MODE:
                log.debug("Purged: %s", label)
        except Exception as e:
            log.debug("Supabase purge (%s): %s", label, str(e)[:60])
    
    # ── Legacy market_key cleanup ──────────────────────────────────────
    for legacy_key in ("totals", "spreads"):
        try:
            sb.table("signals").delete().eq("market_key", legacy_key).execute()
            if DEBUG_MODE:
                log.debug("Purged legacy market_key='%s'", legacy_key)
        except Exception as e:
            log.debug("Supabase purge (legacy %s): %s", legacy_key, str(e)[:60])
    try:
        sb.table("signals").delete().eq("sport", "soccer").eq("market", "Moneyline").execute()
        log.info("Purged: legacy soccer Moneyline")
    except Exception as e:
        log.error("Supabase purge (soccer Moneyline): %s", e)
    # sport-specific past-match purges removed — already covered by global
    # "delete where status=active AND match_time < now" above


def _risk(edge_pct: float) -> str:
    return _risk_flag(edge_pct)


def _emit(signals, sb, now, log, name, sport, league, mkt_key, mkt_label,
          xbet_odd, pin_odd, sharp_prob, emoji, selection_name="", min_edge=None,
          match_time="", match_id="", sharp_sources=None, consensus_score=None,
          ah0_value: bool = False):
    """Compute edge, apply quality gates, collect signal for bulk-save."""
    effective_min = min_edge if min_edge is not None else MIN_EDGE
    edge, status = compute_alpha(xbet_odd, pin_odd, min_edge=effective_min)
    if status == "DISCARD":
        log.info("DISCARD | %s %s | %s — edge %.2f%%", emoji, name, mkt_label, edge)
        return
    if sharp_prob <= 0:
        log.info("DISCARD | %s %s | %s — sharp_prob=0 (stale/missing data)", emoji, name, mkt_label)
        return

    # Safety Trigger: edge trop élevé = probable erreur de données.
    # H2H : seuil strict 10% (risque inversion team mapping).
    # Totals/Spreads : seuil large 15% (pas d'inversion possible, lag légitime sur baseball/KBO).
    suspect_cap = _SUSPECT_EDGE if mkt_key == "h2h" else _SUSPECT_EDGE * 1.5
    if edge > suspect_cap and sport in _MAJOR_SPORTS:
        log.warning("SUSPECT | %s %s | %s — Edge=+%.2f%% > %.0f%% — DISCARD",
                    emoji, name, mkt_label, edge, suspect_cap)
        return

    # J+72h filter: signaux très éloignés doivent être HIGH_VALUE (≥ 6%) pour justifier immobilisation capital
    # Fenêtre portée 36h→72h : capture WC + Copa Lib 2 jours avant match (meilleure liquidité pré-tournoi)
    if match_time:
        try:
            mt_dt = datetime.fromisoformat(match_time.replace("Z", "+00:00"))
            hours_ahead_mt = (mt_dt - now).total_seconds() / 3600
            if hours_ahead_mt > 72 and edge < _ELITE_EDGE * 2:
                log.info("J+3 FILTER | %s %s | %s — edge %.2f%% < 6%% (T+%.0fh)",
                         emoji, name, mkt_label, edge, hours_ahead_mt)
                return
            # MLB Totals lineup filter: starters confirmed ~1h before first pitch.
            # Signals generated >6h before game time are pre-lineup — ERA/matchup
            # not yet priced in. Discard to avoid acting on stale total lines.
            if sport == "baseball" and "totals" in mkt_key and hours_ahead_mt > _MLB_LINEUP_WINDOW_H:
                log.info("MLB LINEUP FILTER | %s | %s — T+%.0fh > 6h (lineup unconfirmed)",
                         name, mkt_label, hours_ahead_mt)
                return
        except (ValueError, OverflowError):
            pass

    # Seuil VALUE sport-spécifique — évite LOW_VALUE invisible sur le dashboard
    if sport == "soccer":
        elite = _SOCCER_ELITE_EDGE        # 1.5% — AH0 marché serré
    elif sport == "basketball":
        elite = _BASKETBALL_ELITE_EDGE    # 2.0% — NBA Finales edges typiques 1.5–2.5%
    else:
        elite = _ELITE_EDGE               # 2.5% — autres sports
    risk = "HIGH_VALUE" if edge >= elite * 2 else ("VALUE" if edge >= elite else "LOW_VALUE")
    # Soccer AH0 Value Rule : si la cote DNB du favori > 1.5, upgrade LOW_VALUE → VALUE
    if ah0_value and risk == "LOW_VALUE":
        risk = "VALUE"

    b = xbet_odd - 1
    kelly_full = (sharp_prob * b - (1 - sharp_prob)) / b if b > 0 else 0.0
    kelly_fraction = _KELLY_FRACTION.get(sport, 0.20)
    kelly_pct  = round(max(0.0, kelly_full * kelly_fraction) * 100, 2)

    advice = (
        f"Edge +{edge:.1f}% détecté — Melbet {xbet_odd:.2f} vs Pinnacle {pin_odd:.2f}. "
        f"Prob.Sharp {int(sharp_prob * 100)}%. "
        f"Melbet n'a pas encore ajusté sa cote sur ce mouvement Sharp."
    )

    # Normalize match_time to ISO UTC (+00:00)
    mt = match_time.replace("Z", "+00:00") if match_time else ""

    log.info("SIGNAL  | %s %s | %s: Melbet=%.3f Pin=%.3f Edge=+%.2f%% Prob=%.0f%% %s",
             emoji, name, mkt_label, xbet_odd, pin_odd, edge, sharp_prob * 100, risk)
    signal = {
        "match":          name,
        "league":         league or "",
        "sport":          sport,
        "market":         mkt_label,
        "market_key":     mkt_key,
        "xbet_odd":       float(xbet_odd),
        "pinnacle_price": float(pin_odd),
        "sharp_prob":     float(sharp_prob),
        "edge_pct":       float(edge),
        "risk_flag":      risk,
        "scanned_at":     now.isoformat(),
        "match_time":     mt,
        "match_id":       match_id,
        "status":         "active",
        "selection_name": selection_name or name,
        "kelly_pct":      kelly_pct,
        "advice":         advice,
        "sharp_sources":  json.dumps(sharp_sources) if sharp_sources else None,
        "consensus_score": consensus_score,
    }
    signals.append(signal)


def _process_h2h(m, name, sport, league, home, away, emoji, signals, sb, now, log, min_edge=None):
    """H2H market: DNB for soccer, Moneyline for NBA/Tennis + Prob.Sharp filter."""
    prob_min    = SHARP_PROB_BY_MARKET.get("h2h_soccer" if sport == "soccer" else "h2h", 0.52)
    sources_found: dict = {}

    if "_oracle_price" in m:
        pin_price = m["_oracle_price"]
        xbet_price, _, xbet_fav = to_binary(m["odds_1xbet"], sport, home, away)
        pin_fav = m.get("_oracle_team", "")
        # Strict Matching: if oracle returned no team name, outcome alignment
        # is unverifiable — discard rather than risk a cross-outcome comparison.
        if not pin_fav:
            log.info("DISCARD | %s %s — oracle team unknown, outcome alignment unverifiable", emoji, name)
            return
        sharp_prob = 1.0  # Oracle already filtered
    else:
        xbet_price, _, xbet_fav = to_binary(m["odds_1xbet"], sport, home, away)
        # Strict Matching: lock Pinnacle lookup to the same position as 1XBet
        # (fav_key "1"=home, "2"=away). Never run to_binary() on Pinnacle
        # independently — it could pick a different favourite and silently
        # compare Como's 1XBet odd against Parma's Pinnacle odd.
        fav_key = "1" if xbet_fav == home else "2"
        opp_key = "2" if fav_key == "1" else "1"
        po = m.get("odds_pinnacle", {})
        if sport == "soccer":
            from core.math_engine import calc_dnb
            # Pinnacle DNB — correct 3-arg formula (fav, opp, draw)
            pin_fav_raw = float(po.get(fav_key, 0) or 0)
            pin_opp_raw = float(po.get(opp_key, 0) or 0)
            pin_draw    = float(po.get("X", 0) or 0)
            pin_price = calc_dnb(pin_fav_raw, pin_opp_raw, pin_draw)
            dnb_other = calc_dnb(pin_opp_raw, pin_fav_raw, pin_draw)

            # Weighted Shin: build source prices for BOTH sides
            source_prices_fav = {"pinnacle": pin_price}
            source_prices_opp = {"pinnacle": dnb_other}
            for src_key, src_name in (("odds_circa", "circa"), ("odds_cris", "cris")):
                so = m.get(src_key) or {}
                s_fav  = float(so.get(fav_key, 0) or 0)
                s_opp  = float(so.get(opp_key, 0) or 0)
                s_draw = float(so.get("X", 0) or 0)
                sp_f = calc_dnb(s_fav, s_opp, s_draw)
                sp_o = calc_dnb(s_opp, s_fav, s_draw)
                if sp_f > 1.01:
                    source_prices_fav[src_name] = sp_f
                if sp_o > 1.01:
                    source_prices_opp[src_name] = sp_o

            con_fav, sources_found, is_volatile, consensus_score = calculate_consensus_price(source_prices_fav, sport)
            if is_volatile:
                log.info("VOLATILE | %s %s — CV>1.2% — DISCARD", emoji, name)
                return
            con_opp, _, _, _ = calculate_consensus_price(source_prices_opp, sport)
            if con_fav > 1.01:
                pin_price = con_fav
            if con_opp > 1.01:
                dnb_other = con_opp
            sharp_prob = devig_prob(pin_price, dnb_other)
        else:
            pin_price = float(po.get(fav_key, 0) or 0)
            opp_price = float(po.get(opp_key, 0) or 0)

            # Weighted Shin: build source prices for BOTH sides
            source_prices_fav = {"pinnacle": pin_price}
            source_prices_opp = {"pinnacle": opp_price}
            for src_key, src_name in (("odds_circa", "circa"), ("odds_cris", "cris")):
                so = m.get(src_key) or {}
                sp_f = float(so.get(fav_key, 0) or 0)
                sp_o = float(so.get(opp_key, 0) or 0)
                if sp_f > 1.01:
                    source_prices_fav[src_name] = sp_f
                if sp_o > 1.01:
                    source_prices_opp[src_name] = sp_o

            con_fav, sources_found, is_volatile, consensus_score = calculate_consensus_price(source_prices_fav, sport)
            if is_volatile:
                log.info("VOLATILE | %s %s — CV>1.2% — DISCARD", emoji, name)
                return
            con_opp, _, _, _ = calculate_consensus_price(source_prices_opp, sport)
            if con_fav > 1.01:
                pin_price = con_fav
            opp_for_shin = con_opp if con_opp > 1.01 else opp_price
            sharp_prob = devig_prob(pin_price, opp_for_shin)

        pin_fav = xbet_fav  # Same outcome guaranteed — no cross-book mismatch possible

    if xbet_price <= 1.01 or pin_price <= 1.01:
        return
    if not strict_team_match(xbet_fav, pin_fav):
        log.info("SPLIT   | %s %s — Melbet=%s Sharp=%s", emoji, name, xbet_fav, pin_fav)
        return
    if sharp_prob < prob_min:
        log.info("LOWPROB | %s %s h2h — Prob.Sharp=%.0f%% < %.0f%%",
                 emoji, name, sharp_prob * 100, prob_min * 100)
        return

    # Soccer AH0 Value Rule : cote DNB fav > 1.5 → signal de valeur intrinsèque
    ah0_value = sport == "soccer" and xbet_price > _AH0_VALUE_THRESHOLD
    # Abaisser le seuil d'edge à 0.8% pour ne pas étouffer ces signaux
    h2h_min_edge = min(min_edge if min_edge is not None else MIN_EDGE, 0.8) if ah0_value else min_edge

    lbl = market_label("h2h", "", 0.0, sport)
    _emit(signals, sb, now, log, name, sport, league,
          "h2h", lbl, xbet_price, pin_price, sharp_prob, emoji,
          selection_name=xbet_fav, min_edge=h2h_min_edge,
          match_time=m.get("commence_time", ""), match_id=m.get("id", ""),
          sharp_sources=sources_found if sources_found else None,
          consensus_score=consensus_score if sources_found else None,
          ah0_value=ah0_value)


def _process_totals(m, name, sport, league, emoji, signals, sb, now, log, min_edge=None):
    """Over/Under market for all sports."""
    prob_min = SHARP_PROB_BY_MARKET["totals"]
    xt = m["totals_1xbet"]
    pt = m["totals_pinnacle"]

    # Line-mismatch guard: 1XBet et Pinnacle sur des totaux différents = faux edge.
    xt_line = float(xt.get("point") or 0)
    pt_line = float(pt.get("point") or 0)
    if xt_line and pt_line and abs(xt_line - pt_line) > 0.5:
        log.info("LINESKIP | %s %s totals — 1XBet %.1f ≠ Pinnacle %.1f",
                 emoji, name, xt_line, pt_line)
        return
    point = pt_line or xt_line

    # Round-line push detection: integer totals (8.0, 9.0) can push.
    # Half-lines (.5) never push — no adjustment needed.
    is_round_line = point > 0 and (point % 1 == 0)
    if is_round_line:
        log.info("ROUNDLINE | %s %s totals %.1f — P(push)=%.0f%% → sharp_prob adjusted",
                 emoji, name, point, _PUSH_PROB_ROUND_LINE * 100)

    for side, other in [("over", "under"), ("under", "over")]:
        x_odd = float(xt.get(side, 0))
        p_odd = float(pt.get(side, 0))
        p_lay = float(pt.get(other, 0))
        if x_odd <= 1.01 or p_odd <= 1.01:
            continue
        sharp_prob = devig_prob(p_odd, p_lay)
        # Push-adjusted probability: P(win | no push) = P(win) / (1 - P(push))
        # This mechanically lowers EV on round lines vs half-lines, as intended.
        if is_round_line:
            sharp_prob = round(sharp_prob * (1 - _PUSH_PROB_ROUND_LINE), 4)
        if sharp_prob < prob_min:
            continue
        lbl = market_label("totals", side, point, sport)
        sel = f"{'Over' if side == 'over' else 'Under'}{(' ' + str(point)) if point else ''}"
        _emit(signals, sb, now, log, name, sport, league,
              f"totals_{side}", lbl, x_odd, p_odd, sharp_prob, emoji,
              selection_name=sel, min_edge=min_edge,
              match_time=m.get("commence_time", ""), match_id=m.get("id", ""))


def _process_spreads(m, name, sport, league, home, away, emoji, signals, sb, now, log, min_edge=None):
    """Spread/Handicap market for NBA + Soccer."""
    prob_min = SHARP_PROB_BY_MARKET["spreads"]
    xs = m["spreads_1xbet"]
    ps = m["spreads_pinnacle"]

    # Line-mismatch guard: handicaps différents entre books = faux edge.
    xs_line = float(xs.get("point") or 0)
    ps_line = float(ps.get("point") or 0)
    if xs_line and ps_line and abs(abs(xs_line) - abs(ps_line)) > 0.5:
        log.info("LINESKIP | %s %s spreads — 1XBet %.1f ≠ Pinnacle %.1f",
                 emoji, name, xs_line, ps_line)
        return
    home_point = float(ps.get("point", xs.get("point", 0.0)))
    away_point = -home_point

    for side, team, pt in [("home", home, home_point), ("away", away, away_point)]:
        x_odd = float(xs.get(side, 0))
        p_odd = float(ps.get(side, 0))
        p_lay = float(ps.get("away" if side == "home" else "home", 0))
        if x_odd <= 1.01 or p_odd <= 1.01:
            continue
        sharp_prob = devig_prob(p_odd, p_lay)
        if sharp_prob < prob_min:
            continue
        lbl = market_label("spreads", side, pt, sport)
        pt_str = f"+{pt}" if pt > 0 else str(pt)
        _emit(signals, sb, now, log, name, sport, league,
              f"spreads_{side}", lbl, x_odd, p_odd, sharp_prob, emoji,
              selection_name=f"{team} {pt_str}", min_edge=min_edge,
              match_time=m.get("commence_time", ""), match_id=m.get("id", ""))


# ── Portfolio Balancer ────────────────────────────────────────────────

def _portfolio_balance(candidates: list) -> list:
    """
    Enforce per-sport quota and sort by edge descending.
    A +5% NBA edge beats a +3% soccer edge even if soccer starts sooner.
    Returns at most SPORT_QUOTA[sport] signals per sport.
    """
    by_sport: dict[str, list] = {}
    for s in sorted(candidates, key=lambda x: x["edge_pct"], reverse=True):
        sport = s.get("sport", "soccer")
        by_sport.setdefault(sport, []).append(s)

    result = []
    for sport in _SPORT_ORDER:
        quota = SPORT_QUOTA.get(sport, 3)
        result.extend(by_sport.get(sport, [])[:quota])
    # Any sport not in _SPORT_ORDER (future-proofing)
    for sport, sigs in by_sport.items():
        if sport not in _SPORT_ORDER:
            result.extend(sigs[:SPORT_QUOTA.get(sport, 3)])
    return result


_RISK_DOT = {"HIGH_VALUE": "🟢", "VALUE": "🟡", "LOW_VALUE": "⚪", "SUSPECT_DATA": "🔴"}


def _urgency_sort(s: dict, now) -> tuple:
    """Sort key: signaux < 4h d'abord (par heure ASC), puis par edge DESC."""
    mt = s.get("match_time") or ""
    try:
        mt_dt = datetime.fromisoformat(mt.replace("Z", "+00:00"))
        mins  = (mt_dt - now).total_seconds() / 60
        if 0 < mins <= 240:
            return (0, mins, -(s.get("edge_pct") or 0))
    except (ValueError, OverflowError):
        pass
    return (1, 9999.0, -(s.get("edge_pct") or 0))
_SESSION_ICON = {"EU-OPEN": "📈", "EU-MID": "⚡", "EU-CLOSE": "🎯", "OVERNIGHT": "🌙"}


def _session_icon(session: str) -> str:
    for k, v in _SESSION_ICON.items():
        if k in session:
            return v
    return ""


def _telegram_grouped(signals: list, now, session: str, matches: int,
                      sharp_source: str, no_pin_count: int):
    """Send all signals grouped by sport, sorted by edge desc."""
    sess      = session.strip()
    icon      = _session_icon(sess)
    estimated = " ⚠️" if sharp_source == "Gemini/Estimateur" else ""
    n_high    = sum(1 for s in signals if s.get("risk_flag") == "HIGH_VALUE")
    n_val     = sum(1 for s in signals if s.get("risk_flag") == "VALUE")
    no_pin    = f" · ❓{no_pin_count}" if no_pin_count > 0 else ""

    if not signals:
        _telegram(
            f"⚫ PREDATOR · {now.strftime('%H:%M')} UTC · {sess} {icon}\n"
            f"0 signaux · {matches} events · {sharp_source}"
        )
        return

    header = (
        f"📡 *PREDATOR* · {now.strftime('%H:%M')} UTC · {sess} {icon}\n"
        f"🟢×{n_high}  🟡×{n_val}  —  {len(signals)} signaux · {matches} events\n"
        f"`{sharp_source}`{estimated}{no_pin}\n"
    )

    # Group by sport — signaux urgents (< 4h) en tête par sport, puis par edge
    by_sport: dict[str, list] = {}
    for s in sorted(signals, key=lambda x: _urgency_sort(x, now)):
        by_sport.setdefault(s["sport"], []).append(s)

    body_parts: list[str] = []
    for sport in _SPORT_ORDER:
        group = by_sport.get(sport, [])
        if not group:
            continue
        emoji = SPORT_EMOJI.get(sport, "🎯")
        lines: list[str] = [f"\n{emoji} *{sport.upper()}*\n"]
        for s in group:
            prob  = s.get("sharp_prob", 0) or 0
            stake = round(_kelly_stake(s["xbet_odd"], prob, sport=s.get("sport", "soccer")))
            team  = s.get("selection_name") or s["match"]
            if " vs " in team:
                team = team.split(" vs ")[0].strip()
            dot   = _RISK_DOT.get(s.get("risk_flag", ""), "⚪")
            mt    = s.get("match_time") or ""
            dt    = f" · {mt[8:10]}/{mt[5:7]} {mt[11:16]}" if mt else ""
            prob_s  = f" · {int(prob * 100)}%" if prob > 0 else ""
            stake_s = f" · *{stake}€/k*" if stake > 0 else ""
            # HIGH VELOCITY: NBA/Soccer < 60 min avant coup d'envoi
            velocity = ""
            if mt:
                try:
                    mt_dt = datetime.fromisoformat(mt.replace("Z", "+00:00"))
                    mins_left = (mt_dt - now).total_seconds() / 60
                    if 0 < mins_left <= 60 and s.get("sport") in {"basketball", "soccer"}:
                        velocity = "🔥 "
                except (ValueError, OverflowError):
                    pass
            lines.append(
                f"{dot} {velocity}*{team.upper()}*  `{s['market']} @ {s['xbet_odd']:.2f}`{dt}\n"
                f"    `+{s['edge_pct']:.1f}%`{prob_s}{stake_s}\n"
            )
        body_parts.append("".join(lines))

    body = "".join(body_parts)
    # Telegram 4096-char limit: send in chunks if needed
    full = header + body
    if len(full) <= 4000:
        _telegram(full)
    else:
        _telegram(header)
        chunk = ""
        for line in body.splitlines(keepends=True):
            if len(chunk) + len(line) > 3800:
                _telegram(chunk)
                chunk = line
            else:
                chunk += line
        if chunk.strip():
            _telegram(chunk)


# ── main ─────────────────────────────────────────────────────────────

def run():
    now     = datetime.now(timezone.utc)
    session = _market_session(now.hour)
    log.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    if GUERRILLA:
        mode = "GUERRILLA 🥷 Sans OddsAPI"
    elif GOLDEN_HOUR:
        mode = "GOLDEN HOUR ⚡ T-120min"
    elif DEEP_SCAN:
        mode = "DEEP 48h"
    else:
        mode = "FAST 72h"
    log.info("PAIM v8.8 — %s | Multi-Sport + Portfolio Balancer | Session: %s", mode, session)
    log.info("Scan start: %s | max_events=%d | quotas=%s",
             now.strftime("%Y-%m-%d %H:%M:%S UTC"), MAX_MATCHES,
             " ".join(f"{k}={v}" for k, v in SPORT_QUOTA.items()))

    try:
        sb = create_client(SUPABASE_URL, SUPABASE_KEY)
        _purge_old_signals(sb)
    except Exception as e:
        log.error("Supabase init failed: %s", e)
        sb = None

    # Load sport-specific MIN_EDGE thresholds from learning layer
    dyn_thresholds: dict[str, float] = {}
    if sb:
        try:
            dyn_thresholds = _load_thresholds(sb)
            if any(v != MIN_EDGE for v in dyn_thresholds.values()):
                log.info("Dynamic thresholds: %s",
                         " | ".join(f"{k}={v:.2f}%" for k, v in dyn_thresholds.items()))
        except Exception as e:
            log.warning("load_thresholds: %s — using default %.1f%%", e, MIN_EDGE)

    # ══ SOURCE PIPELINE — 3 NIVEAUX ══════════════════════════════════
    # Tier 1: The Odds API  → real 1XBet + Pinnacle, même event (idéal)
    # Tier 2: Gemini Search → batch Pinnacle via Google Search
    # Tier 3: Gemini Estim. → probabilités internes, toujours disponible

    matches        = []
    xbet_matches   = []   # declared here so Tier 3 can reuse Tier 2's result safely
    no_pin_count   = 0
    sharp_source   = "?"

    # ── Tier 1: The Odds API ──────────────────────────────────────────
    if GUERRILLA:
        hours_ahead = int(os.environ.get("HOURS_AHEAD", 48))
        log.info("🥷 GUERRILLA — OddsAPI ignoré, Tier 2 direct (1XBet + Pinnacle/Gemini Search)")
    elif GOLDEN_HOUR:
        hours_ahead  = 2  # T-120min window only
        scan_keys    = GOLDEN_SPORT_KEYS
        log.info("⚡ GOLDEN HOUR — OddsAPI (%dh window) | %d sports ciblés: %s",
                 hours_ahead, len(scan_keys), " ".join(scan_keys.keys()))
    else:
        hours_ahead = int(os.environ.get("HOURS_AHEAD", 48 if DEEP_SCAN else 72))
        scan_keys   = None  # Use default SPORT_KEYS (all 38 leagues)
        log.info("⚡ Tier 1 — The Odds API (%dh window)...", hours_ahead)

    if not GUERRILLA:
        oddsapi_events = fetch_odds(hours_ahead=hours_ahead, sport_keys=scan_keys)
        if oddsapi_events:
            matches      = oddsapi_events[:MAX_MATCHES]
            sharp_source = "OddsAPI/Pinnacle"
            sports_found = set(e.get("sport","?") for e in matches)
            log.info("✅ Tier 1 OK — %d/%d events | sports: %s",
                     len(matches), len(oddsapi_events), " ".join(sorted(sports_found)))

        # ── WC Schedule — fetch 168h window + save to meta for worldcup page ──
        if sb and not GOLDEN_HOUR:
            try:
                wc_events = fetch_odds(
                    hours_ahead=168,
                    sport_keys={"soccer_fifa_world_cup": "soccer"},
                )
                if wc_events:
                    wc_fixtures = []
                    for ev in wc_events:
                        pin = ev.get("odds_pinnacle") or {}
                        xbt = ev.get("odds_1xbet") or {}
                        wc_fixtures.append({
                            "match":   ev["match"],
                            "home":    ev["home"],
                            "away":    ev["away"],
                            "time":    ev.get("commence_time", ""),
                            "league":  ev.get("league", "FIFA World Cup 2026"),
                            "pin_1":   pin.get("1", 0),
                            "pin_x":   pin.get("X", 0),
                            "pin_2":   pin.get("2", 0),
                            "xbt_1":   xbt.get("1", 0),
                            "xbt_x":   xbt.get("X", 0),
                            "xbt_2":   xbt.get("2", 0),
                        })
                    sb.table("meta").upsert({
                        "key": "wc_schedule",
                        "value": __import__("json").dumps(wc_fixtures),
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    }).execute()
                    log.info("⚽ WC schedule saved — %d matchs (168h window)", len(wc_fixtures))
            except Exception as e:
                log.warning("WC schedule save: %s", e)

    # ── MMA/eSports/Alternatifs — skipped in Golden Hour (no urgency) ──
    # Cached in Supabase meta: MMA/eSports 8h TTL, alt sports 4h TTL.
    # Saves 3 Gemini Search calls (≈3 RPM) on every Engine scan that hits cache.
    if not GOLDEN_HOUR:
        log.info("🥋 MMA — Gemini Search (Melbet vs Pinnacle)...")
        mma_events = _get_cached(sb, "cache_mma", 8) if sb else None
        if mma_events is None:
            mma_events = fetch_mma_events()
            if mma_events and sb:
                _set_cached(sb, "cache_mma", mma_events)
        if mma_events:
            matches = (matches or []) + mma_events
            log.info("🥋 MMA OK — %d combats UFC (Melbet soft)", len(mma_events))

        log.info("🎮 eSports — Gemini Search (CS2/LoL/Valorant/DOTA2)...")
        esports_events = _get_cached(sb, "cache_esports", 8) if sb else None
        if esports_events is None:
            esports_events = fetch_esports_events()
            if esports_events and sb:
                _set_cached(sb, "cache_esports", esports_events)
        if esports_events:
            matches = (matches or []) + esports_events
            log.info("🎮 eSports OK — %d matchs", len(esports_events))

        log.info("🏓🏐🤾 Sports alternatifs — Gemini Search (Table Tennis / Volleyball / Handball)...")
        alt_events = _get_cached(sb, "cache_altsports", 4) if sb else None
        if alt_events is None:
            alt_events = fetch_alternative_sports_batch()
            if alt_events and sb:
                _set_cached(sb, "cache_altsports", alt_events)
        if alt_events:
            matches = (matches or []) + alt_events
            log.info("🏓🏐🤾 Sports alternatifs OK — %d matchs", len(alt_events))

    # ── Tier 1.5: Betfair Exchange (sharp prices peer-to-peer) ─────────
    # Activé uniquement si BETFAIR_APP_KEY est configuré.
    # Enrichit les matchs dont les prix Pinnacle viennent de l'estimateur Gemini
    # en les remplaçant par des prix Betfair (0% marge avant commission 5%).
    betfair_prices: dict = {}
    if os.environ.get("BETFAIR_APP_KEY"):
        log.info("💹 Tier 1.5 — Betfair Exchange (commission -5%%)...")
        betfair_prices = fetch_betfair_prices(
            sports=["soccer", "tennis", "basketball", "hockey", "mma", "cricket"],
            hours_ahead=hours_ahead,
        )
        if betfair_prices:
            log.info("💹 Betfair OK — %d marchés Betfair chargés", len(betfair_prices))
            enriched = 0
            for m in matches:
                if not m.get("_estimated"):
                    continue
                h   = m.get("home", "").lower().strip()
                a   = m.get("away", "").lower().strip()
                bf  = betfair_prices.get(f"{h}_{a}")
                if not bf:
                    # Essai inversé (away_home)
                    bf_r = betfair_prices.get(f"{a}_{h}")
                    if bf_r:
                        bf = {"1": bf_r["2"], "X": bf_r["X"], "2": bf_r["1"]}
                if bf and bf.get("1", 0) > 1.01 and bf.get("2", 0) > 1.01:
                    m["odds_pinnacle"] = {"1": bf["1"], "X": bf.get("X", 0.0), "2": bf["2"]}
                    m["_betfair"]      = True
                    m.pop("_estimated", None)
                    enriched += 1
                    log.info("💹 Betfair enrichi — %s (%.2f / %.2f)", m["match"], bf["1"], bf["2"])
            if enriched:
                log.info("💹 Betfair: %d matchs enrichis (remplace estimateur Gemini)", enriched)
                if sharp_source == "Gemini/Estimateur":
                    sharp_source = "Betfair+Gemini"
        else:
            log.info("💹 Betfair: 0 marchés (marché vide ou hors session)")

    # ── Golden Hour early-exit — aucun event OddsAPI dans T-2h ──────────
    # Si OddsAPI ne trouve rien dans la fenêtre 2h, les lignes ne bougent pas.
    # Tier 2/3 (Gemini Search) ne sert à rien ici : trop lent, rate-limited,
    # et les prix estimés ont moins de valeur que le vrai mouvement Pinnacle.
    if GOLDEN_HOUR and not matches:
        log.info("⚡ GOLDEN HOUR — 0 events dans T-2h → exit rapide (lignes stables)")
        if sb:
            _heartbeat(sb, now, 0, 0)
        return

    # ── Tier 2: Gemini + Google Search — activé si OddsAPI vide/GUERRILLA ──
    if not matches:
        log.info("📡 Tier 2 — Harvest Melbet + Gemini Search Pinnacle...")
        xbet_matches = fetch_matches()
        if not xbet_matches:
            msg = "📡 PREDATOR v8.8: 0 matchs trouvés — Melbet inaccessible."
            log.warning(msg)
            _telegram(msg)
            if sb:
                _heartbeat(sb, now, 0, 0)
            return

        log.info("%d matchs Melbet | Requête Pinnacle → Gemini Search...", len(xbet_matches))
        pinnacle_map = fetch_pinnacle_prices(xbet_matches)

        MAX_ORACLE = 3
        oracle_used = 0
        for m in xbet_matches[:MAX_MATCHES]:
            pin_odds = pinnacle_map.get(m["match"])
            if pin_odds:
                m["odds_pinnacle"] = pin_odds
                matches.append(m)
            elif oracle_used < MAX_ORACLE:
                sport = m.get("sport", "soccer")
                pin_price, pin_team = get_pinnacle_price(
                    m["match"], sport=sport, league=m.get("league", "")
                )
                if pin_price and pin_price > 1.01:
                    m["_oracle_price"] = pin_price
                    m["_oracle_team"]  = pin_team or ""
                    matches.append(m)
                    oracle_used += 1
                    log.info("ORACLE  | %s — %.3f", m["match"], pin_price)
                else:
                    no_pin_count += 1
                    log.warning("⚠️ %s ignoré : Échec prix Sharp", m["match"])
            else:
                no_pin_count += 1
                log.warning("⚠️ %s ignoré : Échec prix Sharp", m["match"])

        if matches:
            sharp_source = "Gemini/Pinnacle"
            log.info("✅ Tier 2 OK — %d matchs avec prix Sharp", len(matches))

    # ── Tier 3: Gemini Estimateur — fallback direct si Tier 1 vide ───
    if not matches:
        # MMA always calls Gemini above — wait before flooding again
        time.sleep(20)
        log.info("🧠 Tier 3 — Gemini Estimateur (connaissance interne, marge 2%%)...")
        if not xbet_matches:
            xbet_matches = fetch_matches()
        if not xbet_matches:
            msg = "📡 PREDATOR v8.8: 0 matchs — toutes sources épuisées."
            log.warning(msg)
            _telegram(msg)
            if sb:
                _heartbeat(sb, now, 0, 0)
            return
        estimated_map = fetch_estimated_prices(xbet_matches)
        for m in xbet_matches[:MAX_MATCHES]:
            est_odds = estimated_map.get(m["match"])
            if est_odds:
                m["odds_pinnacle"] = est_odds
                m["_estimated"]    = True
                matches.append(m)
        if matches:
            sharp_source = "Gemini/Estimateur"
            log.info("✅ Tier 3 OK — %d matchs estimés (non-arbitrage, value)", len(matches))

    if not matches:
        msg = "📡 PREDATOR v8.8: 0 signaux — toutes sources épuisées."
        log.warning(msg)
        _telegram(msg)
        if sb:
            _heartbeat(sb, now, 0, 0)
        return

    candidates = []

    for m in matches:
        try:
            name     = m["match"]
            sport    = m.get("sport", "soccer")
            league   = m.get("league", "")
            home     = m.get("home", "")
            away     = m.get("away", "")
            emoji    = SPORT_EMOJI.get(sport, "🎯")
            min_edge = dyn_thresholds.get(sport, MIN_EDGE)

            # ── H2H market ───────────────────────────────────────
            _process_h2h(m, name, sport, league, home, away, emoji,
                         candidates, sb, now, log, min_edge=min_edge)

            # ── Totals market (Over/Under) ────────────────────────
            if "totals_1xbet" in m and "totals_pinnacle" in m:
                _process_totals(m, name, sport, league, emoji,
                                candidates, sb, now, log, min_edge=min_edge)

            # ── Spreads market (Handicap) ─────────────────────────
            if "spreads_1xbet" in m and "spreads_pinnacle" in m:
                _process_spreads(m, name, sport, league, home, away, emoji,
                                 candidates, sb, now, log, min_edge=min_edge)

        except Exception as e:
            log.error("Match error [%s]: %s", m.get("match", "?"), e)
            continue

    # ── Portfolio Balancer — apply quota + alpha priority ─────────────
    signals = _portfolio_balance(candidates)
    discarded = len(candidates) - len(signals)
    if discarded:
        log.info("Portfolio Balancer: %d candidates → %d kept (%d quota-trimmed)",
                 len(candidates), len(signals), discarded)
    sport_counts = {}
    for s in signals:
        sport_counts[s.get("sport", "?")] = sport_counts.get(s.get("sport", "?"), 0) + 1
    if sport_counts:
        log.info("Allocation: %s", " | ".join(
            f"{SPORT_EMOJI.get(sp,'🎯')} {sp}={n}" for sp, n in sport_counts.items()))

    # ── B. Bulk-save balanced signals to Supabase ─────────────────────
    saved_count = 0
    if sb and signals:
        for s in signals:
            if _save(sb, s):
                saved_count += 1
        log.info("Supabase: %d/%d signals persisted", saved_count, len(signals))
        if saved_count == 0:
            log.error("Telegram skipped — all %d signals failed to persist", len(signals))

    # ── C. Telegram — toujours envoyé (découplé de Supabase) ──────────
    # Shadow Layer: execution jitter — simule latence humaine (1.5–4.0s)
    time.sleep(random.uniform(1.5, 4.0))
    _telegram_grouped(signals, now, session, len(matches), sharp_source, no_pin_count)

    elite = [s for s in signals if s["edge_pct"] >= ELITE_EDGE]
    log.info("Done. %d candidates | %d balanced | %d elite.",
             len(candidates), len(signals), len(elite))
    log.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    if sb:
        _heartbeat(sb, now, len(matches), len(signals))


if __name__ == "__main__":
    run()
