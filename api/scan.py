"""
api/scan.py — Endpoint FastAPI pour Vercel Serverless + Cron Jobs
GET /api/scan  →  déclenche un scan PAIM complet
"""
from __future__ import annotations

import logging
import os
import math
import time
from typing import Optional

from fastapi import FastAPI
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("api.scan")

app = FastAPI(title="Predator PAIM API", version="1.0.0")


def _get(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


@app.get("/api/scan")
async def run_scan() -> JSONResponse:
    """Déclenché par Vercel Cron — pipeline PAIM complet."""
    logger.info("🕐 Scan PAIM démarré")
    start = time.monotonic()

    odds_api_key = _get("ODDS_API_KEY")
    telegram_token = _get("TELEGRAM_BOT_TOKEN")
    telegram_chat_id = _get("TELEGRAM_CHAT_ID")
    supabase_url = _get("SUPABASE_URL")
    supabase_key = _get("SUPABASE_KEY")

    if not odds_api_key:
        return JSONResponse({"status": "error", "message": "ODDS_API_KEY manquant"}, status_code=500)

    try:
        import httpx
        from supabase import create_client

        # ── 1. Fetch odds ─────────────────────────────────────
        sports = ["soccer_epl", "soccer_ligue_1", "basketball_nba"]
        all_events = []

        async with httpx.AsyncClient(timeout=10.0) as client:
            for sport in sports:
                try:
                    r = await client.get(
                        f"https://api.the-odds-api.com/v4/sports/{sport}/odds/",
                        params={
                            "apiKey": odds_api_key,
                            "regions": "eu",
                            "markets": "h2h",
                            "oddsFormat": "decimal",
                            "bookmakers": "pinnacle,bet365,unibet",
                        },
                    )
                    if r.status_code == 200:
                        all_events.extend(r.json())
                except Exception as e:
                    logger.warning(f"Fetch error {sport}: {e}")

        # ── 2. PAIM Engine (inline — no scipy) ───────────────
        signals = []
        for event in all_events:
            signal = _process_event(event)
            if signal:
                signals.append(signal)

        # Top 9 by EV+
        signals.sort(key=lambda x: x["ev_plus"], reverse=True)
        top = signals[:9]

        # ── 3. Persist to Supabase ────────────────────────────
        if supabase_url and supabase_key and top:
            try:
                db = create_client(supabase_url, supabase_key)
                for s in top:
                    db.table("signals").insert({
                        "event_id": s["event_id"],
                        "event_name": s["event_name"],
                        "sport": s["sport"],
                        "selection": s["selection"],
                        "bookmaker_target": s["bookmaker"],
                        "ev_plus": round(s["ev_plus"], 5),
                        "sharp_prob": round(s["sharp_prob"], 5),
                        "recommended_stake": s["stake"],
                        "status": "pending",
                        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    }).execute()
            except Exception as e:
                logger.error(f"Supabase error: {e}")

        # ── 4. Send Telegram ticket ───────────────────────────
        if telegram_token and telegram_chat_id and top:
            await _send_telegram(telegram_token, telegram_chat_id, top)

        duration = round(time.monotonic() - start, 2)

        if not top:
            return JSONResponse({
                "status": "success",
                "message": "Aucune anomalie EV+ détectée.",
                "events_analyzed": len(all_events),
                "duration_seconds": duration,
            })

        return JSONResponse({
            "status": "success",
            "message": f"Ticket d'Élite envoyé — {len(top)} signaux.",
            "events_analyzed": len(all_events),
            "signals_validated": len(top),
            "duration_seconds": duration,
        })

    except Exception as e:
        logger.error(f"❌ Erreur scan: {e}", exc_info=True)
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.get("/api/health")
async def health() -> JSONResponse:
    return JSONResponse({"status": "ok", "version": "1.0.0"})


# ── PAIM Engine inline (no scipy/sklearn) ─────────────────────────

def _shin_probs(odds: list[float]) -> list[float]:
    """Démargeage additif simplifié (Shin fallback)."""
    total = sum(1 / o for o in odds)
    return [1 / (o * total) for o in odds]


def _compute_ev(true_prob: float, offered_odds: float) -> float:
    return true_prob * (offered_odds - 1) - (1 - true_prob)


def _kelly_stake(prob: float, odds: float, bankroll: float = 10000.0, fraction: float = 0.25) -> float:
    b = odds - 1
    f = max((b * prob - (1 - prob)) / b, 0.0)
    raw = f * fraction * bankroll
    return round(raw / 10) * 10


def _process_event(event: dict) -> Optional[dict]:
    """Extrait le meilleur signal EV+ d'un événement."""
    sharp_bm = next((b for b in event.get("bookmakers", []) if b["key"] == "pinnacle"), None)
    soft_bm = next((b for b in event.get("bookmakers", []) if b["key"] in ("bet365", "unibet")), None)

    if not sharp_bm or not soft_bm:
        return None

    sharp_market = next((m for m in sharp_bm.get("markets", []) if m["key"] == "h2h"), None)
    soft_market = next((m for m in soft_bm.get("markets", []) if m["key"] == "h2h"), None)

    if not sharp_market or not soft_market:
        return None

    sharp_outcomes = sharp_market.get("outcomes", [])
    soft_outcomes = soft_market.get("outcomes", [])

    if len(sharp_outcomes) < 2 or len(soft_outcomes) < 2:
        return None

    sharp_odds = [o["price"] for o in sharp_outcomes[:2]]
    sharp_probs = _shin_probs(sharp_odds)

    best_ev, best_sel, best_odds, best_prob = -999, "", 0.0, 0.0
    for i, outcome in enumerate(soft_outcomes[:2]):
        if i >= len(sharp_probs):
            break
        ev = _compute_ev(sharp_probs[i], outcome["price"])
        if ev > best_ev:
            best_ev = ev
            best_sel = outcome["name"]
            best_odds = outcome["price"]
            best_prob = sharp_probs[i]

    if best_ev < 0.08:
        return None

    return {
        "event_id": event.get("id", ""),
        "event_name": f"{event.get('home_team')} vs {event.get('away_team')}",
        "sport": event.get("sport_key", ""),
        "selection": best_sel,
        "bookmaker": soft_bm["key"],
        "ev_plus": best_ev,
        "sharp_prob": best_prob,
        "stake": _kelly_stake(best_prob, best_odds),
    }


async def _send_telegram(token: str, chat_id: str, signals: list[dict]) -> None:
    """Envoie le ticket système via Telegram."""
    import httpx

    lines = ["🦅 *PREDATOR PAIM — TICKET SYSTÈME*", "─" * 28]
    for i, s in enumerate(signals, 1):
        ev_icon = "🔥" if s["ev_plus"] >= 0.15 else "✅"
        lines.append(
            f"{ev_icon} *#{i}* {s['event_name']}\n"
            f"   ➤ `{s['selection']}` | EV `{s['ev_plus']:.1%}` | Mise `{s['stake']:.0f}€`"
        )
    lines.append(f"\n📋 *Système 7/{len(signals)}* — Profit dès 7 bons résultats")

    text = "\n".join(lines)
    async with httpx.AsyncClient() as client:
        try:
            await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            )
        except Exception as e:
            logger.error(f"Telegram error: {e}")
