"""
run_engine.py — PREDATOR PAIM v7.0 — GitHub Actions Engine
Pipeline: 1XBet Harvest → Shin Edge → Gemini Oracle → Telegram → Supabase
"""
import os
import time
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv
from supabase import create_client

from core.harvester import fetch_matches, shin_edge
from core.oracle import get_pinnacle_price

load_dotenv()

GEMINI_KEY     = os.environ.get("GEMINI_API_KEY")
SUPABASE_URL   = os.environ.get("SUPABASE_URL")
SUPABASE_KEY   = os.environ.get("SUPABASE_KEY")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT  = os.environ.get("TELEGRAM_CHAT_ID")

MIN_EDGE    = 3.0   # % minimum to flag as a signal
MAX_MATCHES = 10    # cap to preserve Gemini free quota


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
        print(f"[Supabase] {e}")


def _risk(edge_pct, pinnacle_found):
    if not pinnacle_found:
        return "NO_DATA"
    if edge_pct >= 8:
        return "HIGH_VALUE"
    if edge_pct >= MIN_EDGE:
        return "VALUE"
    return "LOW"


# ── main ─────────────────────────────────────────────────────────────

def run():
    now = datetime.now(timezone.utc)
    print(f"\n[Engine] Start — {now.isoformat()}")

    try:
        sb = create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as e:
        print(f"[Engine] Supabase failed: {e}")
        sb = None

    matches = fetch_matches()
    if not matches:
        _telegram("⚠️ PREDATOR: Scan 1XBet vide — vérifier le flux.")
        return

    signals = []

    for m in matches[:MAX_MATCHES]:
        try:
            name   = m["match"]
            odds   = m["odds_1xbet"]
            # Take the best (highest) odd as the candidate bet
            best   = max(odds["1"], odds.get("2", 0))

            pinnacle = get_pinnacle_price(name, GEMINI_KEY)
            time.sleep(1)  # rate-limit Gemini

            edge = shin_edge(best, pinnacle)
            risk = _risk(edge, pinnacle is not None)

            print(f"  {name} | 1XBet: {best} | Pinnacle: {pinnacle} | Edge: {edge}% | {risk}")

            signal = {
                "match":          name,
                "league":         m.get("league", "") or "",
                "sport":          "football",
                "xbet_odd":       float(best),
                "pinnacle_price": float(pinnacle) if pinnacle else None,
                "edge_pct":       float(edge),
                "risk_flag":      risk,
                "scanned_at":     now.isoformat(),
                "status":         "active",
            }
            signals.append(signal)

            if edge >= MIN_EDGE and sb:
                _save(sb, signal)

        except Exception as e:
            print(f"  [skip] {m.get('match', '?')}: {e}")
            continue

    # Telegram summary
    value_signals = [s for s in signals if s["edge_pct"] >= MIN_EDGE]
    if value_signals:
        msg = f"🎯 *PREDATOR SIGNALS* — {now.strftime('%H:%M UTC')}\n"
        msg += f"Scan: {len(matches)} matchs | Signaux: {len(value_signals)}\n\n"
        for s in value_signals[:5]:
            msg += (
                f"⚡ *{s['match']}*\n"
                f"   1XBet: `{s['xbet_odd']}` | Pinnacle: `{s['pinnacle_price']}`\n"
                f"   Edge: `+{s['edge_pct']}%` | {s['risk_flag']}\n\n"
            )
        _telegram(msg)
    else:
        _telegram(f"✅ PREDATOR: Scan {now.strftime('%H:%M')} — {len(matches)} matchs, aucun signal ≥{MIN_EDGE}%")

    print(f"[Engine] Done. {len(value_signals)} value signals.")


if __name__ == "__main__":
    run()
