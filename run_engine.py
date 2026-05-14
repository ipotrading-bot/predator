"""
run_engine.py — PREDATOR PAIM v7.5 — GitHub Actions Engine
Real data pipeline: The Odds API → AH 0.0 / Moneyline → Edge [1.5%–15%] → Supabase → Telegram
No scrapers. No hallucinations.
"""
import json
import os
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv
from supabase import create_client

from core.odds_api import fetch_odds
from core.math_engine import to_binary
from core.paim_engine import compute_alpha, MIN_EDGE

load_dotenv()

ODDS_API_KEY   = os.environ.get("ODDS_API_KEY")
SUPABASE_URL   = os.environ.get("SUPABASE_URL")
SUPABASE_KEY   = os.environ.get("SUPABASE_KEY")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT  = os.environ.get("TELEGRAM_CHAT_ID")

ELITE_EDGE  = 2.5   # % — send Telegram alert
MAX_MATCHES = 50

SPORT_EMOJI = {"soccer": "⚽", "tennis": "🎾", "basketball": "🏀"}


# ── helpers ──────────────────────────────────────────────────────────

def _telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT, "text": text, "parse_mode": "Markdown"}, timeout=10)
    except Exception as e:
        print(f"[Telegram] {e}")


def _save(sb, signal):
    try:
        sb.table("signals").insert(signal).execute()
    except Exception as e:
        print(f"[Supabase] insert error: {e}")


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
        print(f"[Supabase] heartbeat error: {e}")


def _purge_old_signals(sb):
    """Remove erroneous signals left from previous engine versions."""
    purge_rules = [
        ("edge_pct",  "gt",  15.0,    "edge > 15%"),
        ("edge_pct",  "lt",  MIN_EDGE, f"edge < {MIN_EDGE}%"),
        ("market",    "is",  "null",   "null market"),
    ]
    for col, op, val, label in purge_rules:
        try:
            q = sb.table("signals").delete()
            getattr(q, op)(col, val).execute()
            print(f"[Engine] Purged: {label}")
        except Exception as e:
            print(f"[Supabase] purge ({label}) error: {e}")
    try:
        sb.table("signals").delete().eq("sport", "soccer").eq("market", "Moneyline").execute()
        print("[Engine] Purged: legacy soccer Moneyline")
    except Exception as e:
        print(f"[Supabase] purge (soccer Moneyline) error: {e}")


def _risk(edge_pct: float) -> str:
    if edge_pct >= ELITE_EDGE * 2:
        return "HIGH_VALUE"
    if edge_pct >= ELITE_EDGE:
        return "VALUE"
    return "LOW_VALUE"


# ── main ─────────────────────────────────────────────────────────────

def run():
    now = datetime.now(timezone.utc)
    print(f"\n[Engine] PAIM v7.5 — The Odds API — {now.isoformat()}")

    try:
        sb = create_client(SUPABASE_URL, SUPABASE_KEY)
        _purge_old_signals(sb)
    except Exception as e:
        print(f"[Engine] Supabase failed: {e}")
        sb = None

    matches = fetch_odds(api_key=ODDS_API_KEY)
    if not matches:
        msg = "⚠️ PREDATOR v7.5: 0 matchs — The Odds API vide ou quota épuisé."
        print(f"[Engine] {msg}")
        _telegram(msg)
        if sb:
            _heartbeat(sb, now, 0, 0)
        return

    signals = []

    for m in matches[:MAX_MATCHES]:
        try:
            name   = m["match"]
            sport  = m.get("sport", "soccer")
            league = m.get("league", "")
            home   = m.get("home", "")
            away   = m.get("away", "")
            emoji  = SPORT_EMOJI.get(sport, "🎯")

            # ── Step 1: Binary Synthesis — 1XBet side ────────────
            xbet_price, market, xbet_fav = to_binary(m["odds_1xbet"], sport, home, away)
            if xbet_price <= 1.01 or market is None:
                print(f"  [SKIP] {emoji} {name} — 1XBet: AH 0.0 impossible (no draw odd)")
                continue

            # ── Step 2: Pinnacle fair price — same side ───────────
            pin_price, _, pin_fav = to_binary(m["odds_pinnacle"], sport, home, away)
            if pin_price <= 1.01:
                print(f"  [SKIP] {emoji} {name} — Pinnacle price invalid")
                continue

            # ── Step 3: Same favorite on both books? ──────────────
            if xbet_fav != pin_fav:
                print(f"  [SPLIT] {emoji} {name} — books disagree: 1XBet={xbet_fav} | Pinnacle={pin_fav}")
                continue

            # ── Step 4: Edge computation [1.5% – 15%] ────────────
            edge, status = compute_alpha(xbet_price, pin_price)
            if status == "DISCARD":
                print(f"  [DISCARD] {emoji} {name} — edge {edge:.2f}% outside window")
                continue

            risk = _risk(edge)
            print(f"  {emoji} {name} | {market} {xbet_fav}: 1XBet={xbet_price} Pinnacle={pin_price} Edge=+{edge}% {risk}")

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
            print(f"  [error] {m.get('match', '?')}: {e}")
            continue

    # ── Telegram report ───────────────────────────────────────────────
    elite = [s for s in signals if s["edge_pct"] >= ELITE_EDGE]
    if elite:
        msg = f"🎯 *PREDATOR v7.5 — SIGNAUX ELITE* — {now.strftime('%H:%M UTC')}\n"
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
            f"✅ PREDATOR v7.5: {now.strftime('%H:%M')} UTC — {len(matches)} matchs scannés "
            f"| Signaux: {len(signals)} | Elite ≥{ELITE_EDGE}%: 0"
        )

    print(f"[Engine] Done. {len(signals)} signals | {len(elite)} elite.")
    if sb:
        _heartbeat(sb, now, len(matches), len(signals))


if __name__ == "__main__":
    run()
