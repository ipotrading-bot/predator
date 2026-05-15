"""
run_engine.py — PREDATOR PAIM v7.5 — Guerrilla Mode
Soft  source : Harvester (1XBet direct JSON feed)
Sharp source : Gemini 2.0 Flash + Google Search → Pinnacle prices
Pipeline     : Harvest → Pinnacle enrich → AH 0.0 binary → Edge [1.5%–15%] → Supabase → Telegram
All timestamps : UTC/GMT — no local-time contamination.
"""
import json
import logging
import os
import time
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv
from supabase import create_client

from core.harvester import fetch_matches, fetch_pinnacle_prices
from core.math_engine import to_binary
from core.paim_engine import compute_alpha, MIN_EDGE

load_dotenv()

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

ELITE_EDGE  = 2.5   # % — send Telegram alert
MAX_MATCHES = 50

SPORT_EMOJI = {"soccer": "⚽", "tennis": "🎾", "basketball": "🏀"}

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


def _save(sb, signal):
    try:
        sb.table("signals").insert(signal).execute()
    except Exception as e:
        log.error("Supabase insert: %s", e)


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
    purge_rules = [
        ("edge_pct",  "gt",  15.0,    "edge > 15%"),
        ("edge_pct",  "lt",  MIN_EDGE, f"edge < {MIN_EDGE}%"),
        ("market",    "is",  "null",   "null market"),
    ]
    for col, op, val, label in purge_rules:
        try:
            q = sb.table("signals").delete()
            getattr(q, op)(col, val).execute()
            log.info("Purged: %s", label)
        except Exception as e:
            log.error("Supabase purge (%s): %s", label, e)
    try:
        sb.table("signals").delete().eq("sport", "soccer").eq("market", "Moneyline").execute()
        log.info("Purged: legacy soccer Moneyline")
    except Exception as e:
        log.error("Supabase purge (soccer Moneyline): %s", e)


def _risk(edge_pct: float) -> str:
    if edge_pct >= ELITE_EDGE * 2:
        return "HIGH_VALUE"
    if edge_pct >= ELITE_EDGE:
        return "VALUE"
    return "LOW_VALUE"


# ── main ─────────────────────────────────────────────────────────────

def run():
    now     = datetime.now(timezone.utc)
    session = _market_session(now.hour)
    log.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    log.info("PAIM v7.5 — Guerrilla Mode | Session: %s", session)
    log.info("Scan start: %s", now.strftime("%Y-%m-%d %H:%M:%S UTC"))

    try:
        sb = create_client(SUPABASE_URL, SUPABASE_KEY)
        _purge_old_signals(sb)
    except Exception as e:
        log.error("Supabase init failed: %s", e)
        sb = None

    # ── Step 1: Soft source — 1XBet via Harvester ────────────────────
    log.info("📡 RECHERCHE D'ALPHA VIA HARVESTER...")
    xbet_matches = fetch_matches()
    if not xbet_matches:
        msg = "📡 RECHERCHE D'ALPHA VIA HARVESTER... 0 matchs trouvés."
        log.warning(msg)
        _telegram(msg)
        if sb:
            _heartbeat(sb, now, 0, 0)
        return

    # ── Step 2: Sharp source — Pinnacle via Gemini 2.0 Flash ─────────
    log.info("%d matchs 1XBet | Requête Pinnacle → Gemini...", len(xbet_matches))
    pinnacle_map = fetch_pinnacle_prices(xbet_matches)

    # Merge: keep only matches where Pinnacle price was found
    matches = []
    for m in xbet_matches[:MAX_MATCHES]:
        pin_odds = pinnacle_map.get(m["match"])
        if not pin_odds:
            log.info("NO-PIN  | %s", m["match"])
            continue
        m["odds_pinnacle"] = pin_odds
        matches.append(m)

    if not matches:
        msg = "📡 RECHERCHE D'ALPHA VIA HARVESTER... Pinnacle introuvable sur ces matchs."
        log.warning(msg)
        _telegram(msg)
        if sb:
            _heartbeat(sb, now, len(xbet_matches), 0)
        return

    signals = []

    for m in matches:
        try:
            name   = m["match"]
            sport  = m.get("sport", "soccer")
            league = m.get("league", "")
            home   = m.get("home", "")
            away   = m.get("away", "")
            emoji  = SPORT_EMOJI.get(sport, "🎯")

            # ── Step 3: Binary Synthesis — 1XBet side ────────────
            xbet_price, market, xbet_fav = to_binary(m["odds_1xbet"], sport, home, away)
            if xbet_price <= 1.01 or market is None:
                log.info("SKIP    | %s %s — AH 0.0 impossible (no draw odd)", emoji, name)
                continue

            # ── Step 4: Pinnacle fair price — same side ───────────
            pin_price, _, pin_fav = to_binary(m["odds_pinnacle"], sport, home, away)
            if pin_price <= 1.01:
                log.info("SKIP    | %s %s — Pinnacle price invalid", emoji, name)
                continue

            # ── Step 5: Same favorite on both books? ──────────────
            if xbet_fav != pin_fav:
                log.info("SPLIT   | %s %s — 1XBet=%s | Pinnacle=%s", emoji, name, xbet_fav, pin_fav)
                continue

            # ── Step 6: Edge computation [1.5% – 15%] ────────────
            edge, status = compute_alpha(xbet_price, pin_price)
            if status == "DISCARD":
                log.info("DISCARD | %s %s — edge %.2f%% outside window", emoji, name, edge)
                continue

            risk = _risk(edge)
            log.info("SIGNAL  | %s %s | %s %s: 1XBet=%.3f Pinnacle=%.3f Edge=+%.2f%% %s",
                     emoji, name, market, xbet_fav, xbet_price, pin_price, edge, risk)

            signal = {
                "match":          name,
                "league":         league or "",
                "sport":          sport,
                "market":         market,
                "xbet_odd":       float(xbet_price),
                "pinnacle_price": float(pin_price),
                "edge_pct":       float(edge),
                "risk_flag":      risk,
                "scanned_at":     now.isoformat(),
                "status":         "active",
            }
            signals.append(signal)
            if sb:
                _save(sb, signal)

        except Exception as e:
            log.error("Match error [%s]: %s", m.get("match", "?"), e)
            continue

    # ── Telegram report ───────────────────────────────────────────────
    elite = [s for s in signals if s["edge_pct"] >= ELITE_EDGE]
    if elite:
        msg = f"🎯 *PREDATOR v7.5 — SIGNAUX ELITE* — {now.strftime('%H:%M UTC')} ({session.strip()})\n"
        msg += f"Scan: {len(matches)} matchs | Elite ≥{ELITE_EDGE}%: {len(elite)}\n\n"
        for s in elite[:5]:
            e = SPORT_EMOJI.get(s["sport"], "🎯")
            msg += (
                f"{e} *{s['match']}*\n"
                f"   `{s['market']}` | 1XBet: `{s['xbet_odd']}` | Pinnacle: `{s['pinnacle_price']}`\n"
                f"   Edge: `+{s['edge_pct']}%` | {s['risk_flag']}\n\n"
            )
        _telegram(msg)
    else:
        _telegram(
            f"✅ PREDATOR v7.5: {now.strftime('%H:%M')} UTC ({session.strip()}) — "
            f"{len(matches)} matchs | Signaux: {len(signals)} | Elite ≥{ELITE_EDGE}%: 0"
        )

    log.info("Done. %d signals | %d elite.", len(signals), len(elite))
    log.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    if sb:
        _heartbeat(sb, now, len(matches), len(signals))


if __name__ == "__main__":
    run()
