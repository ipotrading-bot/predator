"""
run_engine.py — PREDATOR PAIM v7.5 — GitHub Actions Engine
Binary Synthesis + Team Mapping Validation + Hard 15% Edge Cap
Pipeline: Multi-Sport Harvest → AH 0.0 / Moneyline → Pinnacle Oracle → Mapping Check → Supabase → Telegram
"""
import json
import os
import time
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv
from supabase import create_client

from core.harvester import fetch_matches
from core.oracle import get_pinnacle_price
from core.math_engine import to_binary
from core.paim_engine import compute_alpha, strict_team_match

load_dotenv()

GEMINI_KEY     = os.environ.get("GEMINI_API_KEY")
SUPABASE_URL   = os.environ.get("SUPABASE_URL")
SUPABASE_KEY   = os.environ.get("SUPABASE_KEY")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT  = os.environ.get("TELEGRAM_CHAT_ID")

MIN_EDGE    = 1.5   # % — save to dashboard
ELITE_EDGE  = 2.5   # % — send Telegram alert
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
        print(f"[Supabase] Heartbeat error: {e}")


def _purge_bad_signals(sb):
    """Remove all erroneous signals: edge > 15%, null market, or legacy h2h/Moneyline soccer."""
    try:
        sb.table("signals").delete().gt("edge_pct", 15.0).execute()
        print("[Engine] Purged signals with edge > 15%")
    except Exception as e:
        print(f"[Supabase] Purge (edge>15) error: {e}")
    try:
        sb.table("signals").delete().lt("edge_pct", MIN_EDGE).execute()
        print(f"[Engine] Purged signals with edge < {MIN_EDGE}%")
    except Exception as e:
        print(f"[Supabase] Purge (edge<{MIN_EDGE}) error: {e}")
    try:
        sb.table("signals").delete().is_("market", "null").execute()
        print("[Engine] Purged legacy signals with null market")
    except Exception as e:
        print(f"[Supabase] Purge (null market) error: {e}")
    try:
        sb.table("signals").delete().eq("sport", "soccer").eq("market", "Moneyline").execute()
        print("[Engine] Purged legacy soccer signals with Moneyline market")
    except Exception as e:
        print(f"[Supabase] Purge (soccer Moneyline) error: {e}")


def _risk(edge_pct: float, pinnacle_found: bool) -> str:
    if not pinnacle_found:
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
    print(f"\n[Engine] PAIM v7.5 — Binary Synthesis — {now.isoformat()}")

    try:
        sb = create_client(SUPABASE_URL, SUPABASE_KEY)
        _purge_bad_signals(sb)
    except Exception as e:
        print(f"[Engine] Supabase failed: {e}")
        sb = None

    matches = fetch_matches()
    if not matches:
        _telegram("⚠️ PREDATOR: Scan multi-sport vide — tous les flux sont inaccessibles.")
        return

    signals = []

    for m in matches[:MAX_MATCHES]:
        try:
            name   = m["match"]
            sport  = m.get("sport", "soccer")
            league = m.get("league", "")
            emoji  = SPORT_EMOJI.get(sport, "🎯")
            home   = m.get("home", "")
            away   = m.get("away", "")

            # ── Step 1: Binary Synthesis ──────────────────────────
            best, market, xbet_fav = to_binary(m["odds_1xbet"], sport, home, away)
            if best <= 1.01 or market is None:
                print(f"  [REJECTED] {emoji} {name} — AH 0.0 impossible (no draw odd)")
                continue

            # ── Step 2: Pinnacle Oracle ───────────────────────────
            pinnacle, pin_fav = get_pinnacle_price(name, sport=sport, api_key=GEMINI_KEY)
            time.sleep(1)

            # ── Step 3: Mapping Validation ────────────────────────
            # Require Pinnacle team name — empty name = cannot validate = reject
            if not pin_fav:
                print(f"  [REJECT]   {emoji} {name} — Pinnacle team name unknown, mapping unverifiable")
                continue
            if xbet_fav and not strict_team_match(xbet_fav, pin_fav):
                print(f"  [MAPPING]  {emoji} {name} — favorite mismatch: 1XBet={xbet_fav} | Pinnacle={pin_fav}")
                continue

            # ── Step 4: Alpha Computation (hard cap at 15%) ───────
            edge, status = compute_alpha(best, pinnacle)
            if status == "DISCARD":
                print(f"  [DISCARD]  {emoji} {name} — edge {edge}% exceeds 15% cap (data error)")
                continue

            risk = _risk(edge, pinnacle is not None)
            print(
                f"  {emoji} {name} | {market}: {best} "
                f"| Pinnacle ({pin_fav or '?'}): {pinnacle} | Edge: {edge}% | {risk}"
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

    # Telegram — elite signals only
    elite = [s for s in signals if s["edge_pct"] >= ELITE_EDGE]
    if elite:
        msg = f"🎯 *PREDATOR v7.5 — SIGNAUX ELITE* — {now.strftime('%H:%M UTC')}\n"
        msg += f"Scan: {len(matches)} | AH 0.0 + Moneyline | Elite ≥{ELITE_EDGE}%: {len(elite)}\n\n"
        for s in elite[:5]:
            e = SPORT_EMOJI.get(s["sport"], "🎯")
            msg += (
                f"{e} *{s['match']}*\n"
                f"   `{s['market']}` | 1XBet: `{s['xbet_odd']}` | Pinnacle: `{s['pinnacle_price']}`\n"
                f"   Edge: `+{s['edge_pct']}%` | {s['risk_flag']}\n\n"
            )
        _telegram(msg)
    else:
        valid = [s for s in signals if s["edge_pct"] >= MIN_EDGE]
        _telegram(
            f"✅ PREDATOR v7.5: {now.strftime('%H:%M')} — {len(matches)} matchs "
            f"| Dashboard ≥{MIN_EDGE}%: {len(valid)} | Elite ≥{ELITE_EDGE}%: 0"
        )

    valid_count = len([s for s in signals if s["edge_pct"] >= MIN_EDGE])
    print(f"[Engine] Done. {valid_count} signals ≥{MIN_EDGE}% | {len(elite)} elite.")

    if sb:
        _heartbeat(sb, now, len(matches), valid_count)


if __name__ == "__main__":
    run()
