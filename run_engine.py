"""
run_engine.py — PREDATOR PAIM v7.2 — GitHub Actions Engine
Binary Synthesis Doctrine: Soccer = AH 0.0 only. NBA/Tennis = Moneyline only.
Pipeline: Multi-Sport Harvest → Binary Conversion → Gemini Oracle → Supabase → Telegram
"""
import os
import time
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv
from supabase import create_client

from core.harvester import fetch_matches
from core.oracle import get_pinnacle_price
from core.math_engine import to_binary
from core.paim_engine import compute_alpha

load_dotenv()

GEMINI_KEY     = os.environ.get("GEMINI_API_KEY")
SUPABASE_URL   = os.environ.get("SUPABASE_URL")
SUPABASE_KEY   = os.environ.get("SUPABASE_KEY")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT  = os.environ.get("TELEGRAM_CHAT_ID")

MIN_EDGE   = 1.5   # % — save to dashboard
ELITE_EDGE = 2.5   # % — send Telegram alert
MAX_MATCHES = 15

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
        print(f"[Supabase] {e}")


def _cleanup_erroneous_signals(sb):
    """Delete signals with unrealistically high edges — artifacts from old mapping bugs."""
    try:
        sb.table("signals").delete().gt("edge_pct", 25.0).execute()
        print("[Engine] Cleaned up erroneous signals (edge_pct > 25%)")
    except Exception as e:
        print(f"[Supabase] Cleanup error: {e}")


def _risk(edge_pct: float, pinnacle_found: bool, status: str) -> str:
    if status == "SUSPECT":
        return "SUSPECT_DATA"
    if status == "DISCARD" or not pinnacle_found:
        return "NO_DATA"
    if edge_pct >= ELITE_EDGE * 2:
        return "HIGH_VALUE"
    if edge_pct >= ELITE_EDGE:
        return "VALUE"
    if edge_pct >= MIN_EDGE:
        return "LOW_VALUE"
    return "LOW"


# ── main ─────────────────────────────────────────────────────────────

def run():
    now = datetime.now(timezone.utc)
    print(f"\n[Engine] PAIM v7.2 — Binary Synthesis — {now.isoformat()}")

    try:
        sb = create_client(SUPABASE_URL, SUPABASE_KEY)
        _cleanup_erroneous_signals(sb)
    except Exception as e:
        print(f"[Engine] Supabase failed: {e}")
        sb = None

    matches = fetch_matches()
    if not matches:
        _telegram("⚠️ PREDATOR: Scan multi-sport vide — vérifier les flux.")
        return

    signals = []

    for m in matches[:MAX_MATCHES]:
        try:
            name   = m["match"]
            sport  = m.get("sport", "soccer")
            league = m.get("league", "")
            emoji  = SPORT_EMOJI.get(sport, "🎯")

            # ── Binary Synthesis: enforce market type ──────────────
            best, market = to_binary(m["odds_1xbet"], sport)
            if best <= 1.01 or market is None:
                print(f"  [REJECTED] {emoji} {name} — no draw odd, AH 0.0 impossible")
                continue

            pinnacle = get_pinnacle_price(name, sport=sport, api_key=GEMINI_KEY)
            time.sleep(1)

            edge, status = compute_alpha(best, pinnacle)

            if status == "DISCARD":
                print(f"  [DISCARD]  {emoji} {name} — edge {edge}% exceeds MAX (25%)")
                continue

            risk = _risk(edge, pinnacle is not None, status)
            suspect_tag = " ⚠️ SUSPECT" if status == "SUSPECT" else ""

            print(
                f"  {emoji} {name} | {market}: {best} "
                f"| Pinnacle: {pinnacle} | Edge: {edge}%{suspect_tag} | {risk}"
            )

            signal = {
                "match":          name,
                "league":         league or "",
                "sport":          sport,
                "market":         market,
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

    # Telegram — elite signals only (exclude SUSPECT)
    elite = [s for s in signals if s["edge_pct"] >= ELITE_EDGE and s["risk_flag"] != "SUSPECT_DATA"]
    suspect = [s for s in signals if s["risk_flag"] == "SUSPECT_DATA"]

    if elite:
        msg = f"🎯 *PREDATOR v7.2 — SIGNAUX ELITE* — {now.strftime('%H:%M UTC')}\n"
        msg += f"Scan: {len(matches)} | Elite ≥{ELITE_EDGE}%: {len(elite)}\n\n"
        for s in elite[:5]:
            e = SPORT_EMOJI.get(s["sport"], "🎯")
            msg += (
                f"{e} *{s['match']}*\n"
                f"   `{s['market']}` | 1XBet: `{s['xbet_odd']}` | Pinnacle: `{s['pinnacle_price']}`\n"
                f"   Edge: `+{s['edge_pct']}%` | {s['risk_flag']}\n\n"
            )
        _telegram(msg)
    else:
        low = [s for s in signals if s["edge_pct"] >= MIN_EDGE and s["risk_flag"] != "SUSPECT_DATA"]
        note = f" | ⚠️ Suspects: {len(suspect)}" if suspect else ""
        _telegram(
            f"✅ PREDATOR v7.2: {now.strftime('%H:%M')} — {len(matches)} matchs "
            f"| Dashboard ≥{MIN_EDGE}%: {len(low)} | Elite ≥{ELITE_EDGE}%: 0{note}"
        )

    valid = [s for s in signals if s["edge_pct"] >= MIN_EDGE and s["risk_flag"] != "SUSPECT_DATA"]
    print(f"[Engine] Done. {len(valid)} valid ≥{MIN_EDGE}% | {len(elite)} elite | {len(suspect)} suspect.")


if __name__ == "__main__":
    run()
