"""
run_engine.py — PREDATOR PAIM v8.5 — Hunter Multi-Sport + Portfolio Balancer
Markets: h2h (NBA/Tennis) | spreads (NBA/Soccer) | totals (all)
Sharp filter: Prob. Sharp (Shin devigged) >= threshold per market type
Pipeline: OddsAPI → Gemini Search → Gemini Estimator → AH0.0/ML/PS/OU → Edge → Balancer → Supabase
All timestamps : UTC/GMT — no local-time contamination.
"""
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv
from supabase import create_client

from core.harvester import fetch_matches, fetch_pinnacle_prices, fetch_estimated_prices, fetch_mma_events, fetch_esports_events
from core.math_engine import to_binary, devig_prob
from core.odds_api import fetch_odds
from core.oracle import get_pinnacle_price
from core.learning_layer import load_thresholds as _load_thresholds
from core.paim_engine import (
    compute_alpha, MIN_EDGE, strict_team_match,
    market_label, SHARP_PROB_BY_MARKET,
)
from core.constants import ELITE_EDGE as _ELITE_EDGE, kelly_stake as _kelly_stake, risk_flag as _risk_flag, SUSPECT_EDGE as _SUSPECT_EDGE

load_dotenv()

# ── Deep-scan mode (DEEP_SCAN=1) ─────────────────────────────────────
# Triggered by .github/workflows/deep_scan.yml or manually.
# Lifts per-sport quotas + scans 48h ahead with up to 100 events.
DEEP_SCAN = os.environ.get("DEEP_SCAN", "0") == "1"

# ── UTC logger ────────────────────────────────────────────────────────
_fmt = logging.Formatter(
    fmt="%(asctime)s UTC | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
_fmt.converter = time.gmtime          # Force UTC — ignore server local time
_handler = logging.StreamHandler()
_handler.setFormatter(_fmt)
log = logging.getLogger("PREDATOR")
log.setLevel(logging.INFO)
log.addHandler(_handler)
log.propagate = False

SUPABASE_URL   = os.environ.get("SUPABASE_URL")
SUPABASE_KEY   = os.environ.get("SUPABASE_KEY")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT  = os.environ.get("TELEGRAM_CHAT_ID")

ELITE_EDGE  = _ELITE_EDGE   # % — send Telegram alert (from core.constants)
_MAJOR_SPORTS = {"soccer", "basketball", "tennis", "hockey", "esports"}  # Sports where edge > SUSPECT_EDGE is flagged

# Fast mode (default): 20 events, tight quota — speed over coverage
# Deep mode (DEEP_SCAN=1): 100 events, wide quota — 48h full slate
MAX_MATCHES = 100 if DEEP_SCAN else 20

SPORT_EMOJI  = {"soccer": "⚽", "tennis": "🎾", "basketball": "🏀", "boxing": "🥊", "mma": "🥋", "darts": "🎯", "cricket": "🏏", "hockey": "🏒", "esports": "🎮"}

# Portfolio Balancer: max signals per sport per scan — prevents soccer flooding
_QUOTA_FAST = {"soccer": 3, "basketball": 3, "hockey": 3, "tennis": 3, "boxing": 3, "mma": 3, "darts": 2, "cricket": 2, "esports": 2}
_QUOTA_DEEP = {"soccer": 6, "basketball": 8, "hockey": 6, "tennis": 10, "boxing": 5, "mma": 5, "darts": 4, "cricket": 4, "esports": 4}
SPORT_QUOTA = _QUOTA_DEEP if DEEP_SCAN else _QUOTA_FAST
# Telegram report order: highest alpha first
_SPORT_ORDER = ["basketball", "hockey", "esports", "mma", "darts", "boxing", "cricket", "tennis", "soccer"]

# EU market sessions (UTC) — aligns with European bookmaker line movement
_SESSIONS = {
    (6,  12): "EU-OPEN  📈",   # 06:00–11:59 UTC — fresh lines, max inefficiency
    (12, 18): "EU-MID   ⚡",   # 12:00–17:59 UTC — afternoon fixtures
    (18, 22): "EU-CLOSE 🎯",   # 18:00–21:59 UTC — prime-time kickoffs
}

def _market_session(hour_utc: int) -> str:
    for (start, end), label in _SESSIONS.items():
        if start <= hour_utc < end:
            return label
    return "OVERNIGHT 🌙"      # 22:00–05:59 UTC — NBA / off-peak


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
_OPTIONAL_COLS = {"selection_name", "kelly_pct", "advice"}


def _save(sb, signal) -> bool:
    """Delete-then-insert to avoid duplicates. Returns True on success."""
    payload = dict(signal)
    # Delete by match_id+market_key first (most precise), then fallback to match+market
    # No status filter — replaces stale signals from all previous scans
    mid  = payload.get("match_id", "")
    mkey = payload.get("market_key", "")
    try:
        if mid and mkey:
            sb.table("signals").delete().eq("match_id", mid).eq("market_key", mkey).execute()
        else:
            sb.table("signals").delete().eq("match", payload["match"]).eq("market", payload.get("market", "")).execute()
    except Exception:
        pass  # Non-fatal — insert will still proceed
    try:
        sb.table("signals").insert(payload).execute()
        return True
    except Exception as e:
        if any(c in str(e) for c in _OPTIONAL_COLS):
            core = {k: v for k, v in payload.items() if k not in _OPTIONAL_COLS}
            try:
                sb.table("signals").insert(core).execute()
                return True
            except Exception as e2:
                log.error("Supabase insert: %s", e2)
        else:
            log.error("Supabase insert: %s", e)
        return False


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
        }).execute()
    except Exception as e:
        log.error("Supabase heartbeat: %s", e)


def _purge_old_signals(sb):
    now_iso = datetime.now(timezone.utc).isoformat()

    # ── Clean Before Scan — matches already started or status=pending ──
    try:
        sb.table("signals").delete().eq("status", "pending").execute()
        log.info("Purged: status=pending")
    except Exception as e:
        log.error("Supabase purge (pending): %s", e)
    try:
        sb.table("signals").delete().eq("status", "active").lt("match_time", now_iso).execute()
        log.info("Purged: active signals with match_time in the past")
    except Exception as e:
        log.error("Supabase purge (past match_time): %s", e)

    # ── Age-based purge — keep last 48h only ──────────────────────────
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
        sb.table("signals").delete().lt("created_at", cutoff).execute()
        log.info("Purged: signals older than 48h")
    except Exception as e:
        log.error("Supabase purge (age): %s", e)

    purge_rules = [
        ("gt",  "edge_pct",   15.0,    "edge > 15% (hard cap)"),
        ("gt",  "edge_pct",   10.0,    "edge > 10% (SUSPECT — probable inversion)"),
        ("lt",  "edge_pct",   MIN_EDGE, f"edge < {MIN_EDGE}%"),
        ("lte", "sharp_prob", 0.0,      "sharp_prob=0 (stale)"),
    ]
    for op, col, val, label in purge_rules:
        try:
            getattr(sb.table("signals").delete(), op)(col, val).execute()
            log.info("Purged: %s", label)
        except Exception as e:
            log.error("Supabase purge (%s): %s", label, e)
    try:
        sb.table("signals").delete().is_("market", "null").execute()
        log.info("Purged: null market")
    except Exception as e:
        log.error("Supabase purge (null market): %s", e)
    try:
        sb.table("signals").delete().is_("sharp_prob", "null").execute()
        log.info("Purged: sharp_prob=null")
    except Exception as e:
        log.error("Supabase purge (sharp_prob=null): %s", e)
    try:
        sb.table("signals").delete().eq("risk_flag", "SUSPECT_DATA").execute()
        log.info("Purged: SUSPECT_DATA signals (edge > 10%% inversion probable)")
    except Exception as e:
        log.error("Supabase purge (SUSPECT_DATA): %s", e)
    try:
        sb.table("signals").delete().lte("xbet_odd", 1.01).execute()
        log.info("Purged: xbet_odd <= 1.01 (signal tronqué)")
    except Exception as e:
        log.error("Supabase purge (xbet_odd): %s", e)
    try:
        sb.table("signals").delete().lte("pinnacle_price", 1.01).execute()
        log.info("Purged: pinnacle_price <= 1.01 (signal tronqué)")
    except Exception as e:
        log.error("Supabase purge (pinnacle_price): %s", e)
    try:
        sb.table("signals").delete().eq("sport", "soccer").eq("market", "Moneyline").execute()
        log.info("Purged: legacy soccer Moneyline")
    except Exception as e:
        log.error("Supabase purge (soccer Moneyline): %s", e)
    try:
        sb.table("signals").delete().eq("sport", "boxing").lt("match_time", datetime.now(timezone.utc).isoformat()).execute()
        log.info("Purged: past boxing matches")
    except Exception as e:
        log.error("Supabase purge (past boxing): %s", e)
    try:
        sb.table("signals").delete().eq("sport", "mma").lt("match_time", datetime.now(timezone.utc).isoformat()).execute()
        log.info("Purged: past MMA fights")
    except Exception as e:
        log.error("Supabase purge (past MMA): %s", e)
    for _dead_sport in ("darts", "cricket"):
        try:
            sb.table("signals").delete().eq("sport", _dead_sport).lt("match_time", datetime.now(timezone.utc).isoformat()).execute()
            log.info("Purged: past %s events", _dead_sport)
        except Exception as e:
            log.error("Supabase purge (past %s): %s", _dead_sport, e)


def _risk(edge_pct: float) -> str:
    return _risk_flag(edge_pct)


def _emit(signals, sb, now, log, name, sport, league, mkt_key, mkt_label,
          xbet_odd, pin_odd, sharp_prob, emoji, selection_name="", min_edge=None,
          match_time="", match_id=""):
    """Compute edge, apply quality gates, collect signal for bulk-save."""
    effective_min = min_edge if min_edge is not None else MIN_EDGE
    edge, status = compute_alpha(xbet_odd, pin_odd, min_edge=effective_min)
    if status == "DISCARD":
        log.info("DISCARD | %s %s | %s — edge %.2f%%", emoji, name, mkt_label, edge)
        return
    if sharp_prob <= 0:
        log.info("DISCARD | %s %s | %s — sharp_prob=0 (stale/missing data)", emoji, name, mkt_label)
        return

    # Safety Trigger: edge > 10% on a major sport = data mapping error.
    # Never save to DB — alert only, then discard.
    if edge > _SUSPECT_EDGE and sport in _MAJOR_SPORTS:
        log.warning("SUSPECT | %s %s | %s — Edge=+%.2f%% > 10%% — DISCARD (erreur données probable)",
                    emoji, name, mkt_label, edge)
        return
    risk = _risk(edge)

    b = xbet_odd - 1
    kelly_full = (sharp_prob * b - (1 - sharp_prob)) / b if b > 0 else 0.0
    kelly_pct  = round(max(0.0, kelly_full * 0.25) * 100, 2)

    advice = (
        f"Edge +{edge:.1f}% détecté — 1XBet {xbet_odd:.2f} vs Pinnacle {pin_odd:.2f}. "
        f"Prob.Sharp {int(sharp_prob * 100)}%. "
        f"1XBet n'a pas encore ajusté sa cote sur ce mouvement Sharp."
    )

    # Normalize match_time to ISO UTC (+00:00)
    mt = match_time.replace("Z", "+00:00") if match_time else ""

    log.info("SIGNAL  | %s %s | %s: 1XBet=%.3f Pin=%.3f Edge=+%.2f%% Prob=%.0f%% %s",
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
    }
    signals.append(signal)


def _process_h2h(m, name, sport, league, home, away, emoji, signals, sb, now, log, min_edge=None):
    """H2H market: DNB for soccer, Moneyline for NBA/Tennis + Prob.Sharp filter."""
    prob_min = SHARP_PROB_BY_MARKET.get("h2h_soccer" if sport == "soccer" else "h2h", 0.52)

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
            pin_price = calc_dnb(float(po.get(fav_key, 0) or 0), float(po.get("X", 0) or 0))
            dnb_other = calc_dnb(float(po.get(opp_key, 0) or 0), float(po.get("X", 0) or 0))
            sharp_prob = devig_prob(pin_price, dnb_other)
        else:
            pin_price  = float(po.get(fav_key, 0) or 0)
            sharp_prob = devig_prob(pin_price, float(po.get(opp_key, 0) or 0))
        pin_fav = xbet_fav  # Same outcome guaranteed — no cross-book mismatch possible

    if xbet_price <= 1.01 or pin_price <= 1.01:
        return
    if not strict_team_match(xbet_fav, pin_fav):
        log.info("SPLIT   | %s %s — 1XBet=%s Sharp=%s", emoji, name, xbet_fav, pin_fav)
        return
    if sharp_prob < prob_min:
        log.info("LOWPROB | %s %s h2h — Prob.Sharp=%.0f%% < %.0f%%",
                 emoji, name, sharp_prob * 100, prob_min * 100)
        return

    lbl = market_label("h2h", "", 0.0, sport)
    _emit(signals, sb, now, log, name, sport, league,
          "h2h", lbl, xbet_price, pin_price, sharp_prob, emoji,
          selection_name=xbet_fav, min_edge=min_edge,
          match_time=m.get("commence_time", ""), match_id=m.get("id", ""))


def _process_totals(m, name, sport, league, emoji, signals, sb, now, log, min_edge=None):
    """Over/Under market for all sports."""
    prob_min = SHARP_PROB_BY_MARKET["totals"]
    xt = m["totals_1xbet"]
    pt = m["totals_pinnacle"]
    point = pt.get("point", xt.get("point", 0.0))

    for side, other in [("over", "under"), ("under", "over")]:
        x_odd = float(xt.get(side, 0))
        p_odd = float(pt.get(side, 0))
        p_lay = float(pt.get(other, 0))
        if x_odd <= 1.01 or p_odd <= 1.01:
            continue
        sharp_prob = devig_prob(p_odd, p_lay)
        if sharp_prob < prob_min:
            continue
        lbl = market_label("totals", side, point, sport)
        sel = f"{'Over' if side == 'over' else 'Under'}{(' ' + str(point)) if point else ''}"
        _emit(signals, sb, now, log, name, sport, league,
              "totals", lbl, x_odd, p_odd, sharp_prob, emoji,
              selection_name=sel, min_edge=min_edge,
              match_time=m.get("commence_time", ""), match_id=m.get("id", ""))


def _process_spreads(m, name, sport, league, home, away, emoji, signals, sb, now, log, min_edge=None):
    """Spread/Handicap market for NBA + Soccer."""
    prob_min = SHARP_PROB_BY_MARKET["spreads"]
    xs = m["spreads_1xbet"]
    ps = m["spreads_pinnacle"]
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
              "spreads", lbl, x_odd, p_odd, sharp_prob, emoji,
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

    # Group ALL signals by sport, sorted by edge desc within each sport
    by_sport: dict[str, list] = {}
    for s in sorted(signals, key=lambda x: x["edge_pct"], reverse=True):
        by_sport.setdefault(s["sport"], []).append(s)

    body = ""
    for sport in _SPORT_ORDER:
        group = by_sport.get(sport, [])
        if not group:
            continue
        emoji = SPORT_EMOJI.get(sport, "🎯")
        body += f"\n{emoji} *{sport.upper()}*\n"
        for s in group:
            prob  = s.get("sharp_prob", 0) or 0
            stake = _kelly_stake(s["xbet_odd"], prob)
            team  = s.get("selection_name") or s["match"]
            if " vs " in team:
                team = team.split(" vs ")[0].strip()
            dot   = _RISK_DOT.get(s.get("risk_flag", ""), "⚪")
            mt    = s.get("match_time") or ""
            dt    = f" · {mt[8:10]}/{mt[5:7]} {mt[11:16]}" if mt else ""
            prob_s  = f" · {int(prob * 100)}%" if prob > 0 else ""
            stake_s = f" · *{stake}€/k*" if stake > 0 else ""
            body += (
                f"{dot} *{team.upper()}*  `{s['market']} @ {s['xbet_odd']:.2f}`{dt}\n"
                f"    `+{s['edge_pct']:.1f}%`{prob_s}{stake_s}\n"
            )

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
    mode = "DEEP 48h" if DEEP_SCAN else "FAST 72h"
    log.info("PAIM v8.5 — %s | Multi-Sport + Portfolio Balancer | Session: %s", mode, session)
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
    hours_ahead = int(os.environ.get("HOURS_AHEAD", 48 if DEEP_SCAN else 72))
    log.info("⚡ Tier 1 — The Odds API (%dh window)...", hours_ahead)
    oddsapi_events = fetch_odds(hours_ahead=hours_ahead)
    if oddsapi_events:
        matches      = oddsapi_events[:MAX_MATCHES]
        sharp_source = "OddsAPI/Pinnacle"
        sports_found = set(e.get("sport","?") for e in matches)
        log.info("✅ Tier 1 OK — %d/%d events | sports: %s",
                 len(matches), len(oddsapi_events), " ".join(sorted(sports_found)))

    # ── MMA/UFC — Gemini Search (Melbet vs Pinnacle) — always runs ────
    log.info("🥋 MMA — Gemini Search (Melbet vs Pinnacle)...")
    mma_events = fetch_mma_events()
    if mma_events:
        matches = (matches or []) + mma_events
        log.info("🥋 MMA OK — %d combats UFC (Melbet soft)", len(mma_events))

    # ── eSports — Gemini Search (1XBet vs Pinnacle) — always runs ────
    log.info("🎮 eSports — Gemini Search (CS2/LoL/Valorant/DOTA2)...")
    esports_events = fetch_esports_events()
    if esports_events:
        matches = (matches or []) + esports_events
        log.info("🎮 eSports OK — %d matchs", len(esports_events))

    # ── Tier 2: Gemini + Google Search — DISABLED (trop lent) ───────
    # Saute directement en Tier 3 (Estimateur) si Tier 1 vide
    if False and not matches:
        log.info("📡 Tier 2 — Harvest 1XBet + Gemini Search Pinnacle...")
        xbet_matches = fetch_matches()
        if not xbet_matches:
            msg = "📡 PREDATOR v8.5: 0 matchs trouvés — 1XBet inaccessible."
            log.warning(msg)
            _telegram(msg)
            if sb:
                _heartbeat(sb, now, 0, 0)
            return

        log.info("%d matchs 1XBet | Requête Pinnacle → Gemini Search...", len(xbet_matches))
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
            msg = "📡 PREDATOR v8.5: 0 matchs — toutes sources épuisées."
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
        msg = "📡 PREDATOR v8.5: 0 signaux — toutes sources épuisées."
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

    # ── C. Telegram UNIQUEMENT si persistance Supabase réussie ─────────
    if not signals or saved_count > 0:
        _telegram_grouped(signals, now, session, len(matches), sharp_source, no_pin_count)

    elite = [s for s in signals if s["edge_pct"] >= ELITE_EDGE]
    log.info("Done. %d candidates | %d balanced | %d elite.",
             len(candidates), len(signals), len(elite))
    log.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    if sb:
        _heartbeat(sb, now, len(matches), len(signals))


if __name__ == "__main__":
    run()
