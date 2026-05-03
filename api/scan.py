"""
api/scan.py — Predator PAIM — Vercel Serverless API
"""
from __future__ import annotations

import logging
import os
import time
from typing import Optional

from fastapi import FastAPI
from fastapi.responses import JSONResponse, HTMLResponse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("api.scan")

app = FastAPI(title="Predator PAIM API", version="2.0.0")


def _get(key: str) -> str:
    return os.environ.get(key, "")


# ── HTML Terminal UI ──────────────────────────────────────────────

HTML = """<!DOCTYPE html>
<html lang="fr">
<head>
    <meta charset="UTF-8">
    <title>PREDATOR PAIM | Terminal</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { background: #0a0a0a; color: #00ff00; font-family: 'Courier New', monospace; padding: 24px; }
        .container { border: 1px solid #00ff00; padding: 24px; border-radius: 6px; box-shadow: 0 0 20px #00ff0022; max-width: 900px; margin: auto; }
        h1 { border-bottom: 1px solid #00ff00; padding-bottom: 12px; margin-bottom: 20px; font-size: 1.4rem; }
        .stats { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 16px; margin-bottom: 24px; }
        .card { border: 1px solid #1a1a1a; padding: 14px; background: #111; border-radius: 4px; font-size: 0.9rem; }
        .green { color: #00ff00; font-weight: bold; }
        .btn { display: inline-block; background: #00ff00; color: #000; padding: 10px 22px; border-radius: 4px; font-weight: bold; cursor: pointer; text-decoration: none; font-family: 'Courier New', monospace; border: none; font-size: 0.95rem; }
        .btn:hover { background: #00cc00; }
        h3 { margin: 24px 0 12px; border-bottom: 1px solid #1a1a1a; padding-bottom: 8px; }
        #result { margin-top: 16px; padding: 14px; background: #111; border: 1px solid #1a1a1a; border-radius: 4px; min-height: 60px; white-space: pre-wrap; font-size: 0.85rem; }
        .loading { color: #888; }
    </style>
</head>
<body>
    <div class="container">
        <h1>🦅 PREDATOR PAIM v2.0 <span style="font-size:11px;float:right;color:#888;">PHD MIT QUANT SYSTEM</span></h1>
        <div class="stats">
            <div class="card">STATUT: <span class="green">ACTIF</span></div>
            <div class="card">CAPITAL: 10,000 €</div>
            <div class="card">TARGET: EV+ &gt; 8%</div>
        </div>
        <button class="btn" onclick="triggerScan()">⚡ DÉCLENCHER SCAN MANUEL</button>
        <h3>RÉSULTAT DU DERNIER SCAN</h3>
        <div id="result" class="loading">En attente du scan...</div>
    </div>
    <script>
        async function triggerScan() {
            const el = document.getElementById('result');
            el.className = 'loading';
            el.textContent = '⏳ Scan en cours...';
            try {
                const r = await fetch('/api/scan');
                const data = await r.json();
                el.className = '';
                el.textContent = JSON.stringify(data, null, 2);
            } catch(e) {
                el.textContent = '❌ Erreur: ' + e.message;
            }
        }
    </script>
</body>
</html>"""


# ── Routes ────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def root() -> HTMLResponse:
    return HTMLResponse(content=HTML)


@app.get("/api/health")
async def health() -> JSONResponse:
    return JSONResponse({"status": "ok", "version": "2.0.0"})


@app.get("/api/scan")
async def run_scan() -> JSONResponse:
    """Pipeline PAIM complet — déclenché par cron ou manuellement."""
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

        # 1. Fetch odds
        sports = ["soccer_epl", "soccer_ligue_1", "basketball_nba"]
        all_events: list[dict] = []

        async with httpx.AsyncClient(timeout=15.0) as client:
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

        # 2. PAIM Engine
        signals = [s for e in all_events if (s := _process_event(e))]
        signals.sort(key=lambda x: x["ev_plus"], reverse=True)
        top = signals[:9]

        # 3. Persist to Supabase
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

        # 4. Telegram
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
            "top_signals": [{"event": s["event_name"], "selection": s["selection"], "ev": f"{s['ev_plus']:.1%}", "stake": f"{s['stake']:.0f}€"} for s in top],
            "duration_seconds": duration,
        })

    except Exception as e:
        logger.error(f"❌ Erreur: {e}", exc_info=True)
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


# ── PAIM Engine (inline) ──────────────────────────────────────────

def _shin_probs(odds: list[float]) -> list[float]:
    total = sum(1 / o for o in odds)
    return [1 / (o * total) for o in odds]


def _compute_ev(prob: float, odds: float) -> float:
    return prob * (odds - 1) - (1 - prob)


def _kelly_stake(prob: float, odds: float, bankroll: float = 10000.0, fraction: float = 0.25) -> float:
    b = odds - 1
    f = max((b * prob - (1 - prob)) / b, 0.0)
    return round(f * fraction * bankroll / 10) * 10


def _process_event(event: dict) -> Optional[dict]:
    sharp = next((b for b in event.get("bookmakers", []) if b["key"] == "pinnacle"), None)
    soft = next((b for b in event.get("bookmakers", []) if b["key"] in ("bet365", "unibet")), None)
    if not sharp or not soft:
        return None

    sm = next((m for m in sharp.get("markets", []) if m["key"] == "h2h"), None)
    fm = next((m for m in soft.get("markets", []) if m["key"] == "h2h"), None)
    if not sm or not fm:
        return None

    so = sm.get("outcomes", [])
    fo = fm.get("outcomes", [])
    if len(so) < 2 or len(fo) < 2:
        return None

    probs = _shin_probs([o["price"] for o in so[:2]])
    best_ev, best_sel, best_odds, best_prob = -999.0, "", 0.0, 0.0

    for i, outcome in enumerate(fo[:2]):
        if i >= len(probs):
            break
        ev = _compute_ev(probs[i], outcome["price"])
        if ev > best_ev:
            best_ev, best_sel, best_odds, best_prob = ev, outcome["name"], outcome["price"], probs[i]

    if best_ev < 0.08:
        return None

    return {
        "event_id": event.get("id", ""),
        "event_name": f"{event.get('home_team')} vs {event.get('away_team')}",
        "sport": event.get("sport_key", ""),
        "selection": best_sel,
        "bookmaker": soft["key"],
        "ev_plus": best_ev,
        "sharp_prob": best_prob,
        "stake": _kelly_stake(best_prob, best_odds),
    }


async def _send_telegram(token: str, chat_id: str, signals: list[dict]) -> None:
    import httpx
    lines = ["🦅 *PREDATOR PAIM — TICKET SYSTÈME*", "─" * 28]
    for i, s in enumerate(signals, 1):
        icon = "🔥" if s["ev_plus"] >= 0.15 else "✅"
        lines.append(f"{icon} *#{i}* {s['event_name']}\n   ➤ `{s['selection']}` | EV `{s['ev_plus']:.1%}` | Mise `{s['stake']:.0f}€`")
    lines.append(f"\n📋 *Système 7/{len(signals)}* — Profit dès 7 bons résultats")
    async with httpx.AsyncClient() as client:
        try:
            await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": "\n".join(lines), "parse_mode": "Markdown"},
            )
        except Exception as e:
            logger.error(f"Telegram error: {e}")
