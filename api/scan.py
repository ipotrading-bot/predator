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
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>PREDATOR PAIM | Terminal</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { background: #0a0a0a; color: #00ff00; font-family: 'Courier New', monospace; padding: 24px; }
        .container { border: 1px solid #00ff00; padding: 24px; border-radius: 6px;
                     box-shadow: 0 0 20px #00ff0022; max-width: 960px; margin: auto; }
        h1 { border-bottom: 1px solid #00ff00; padding-bottom: 12px; margin-bottom: 20px; font-size: 1.3rem; }
        h3 { margin: 24px 0 12px; color: #00cc00; font-size: 1rem; }
        .stats { display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; margin-bottom: 24px; }
        .card { border: 1px solid #1a3a1a; padding: 14px; background: #0f1f0f; border-radius: 4px; font-size: 0.85rem; }
        .card span { color: #00ff00; font-weight: bold; }
        .btn { display: inline-block; background: #00ff00; color: #000; padding: 10px 22px;
               border-radius: 4px; font-weight: bold; cursor: pointer; border: none;
               font-family: 'Courier New', monospace; font-size: 0.9rem; }
        .btn:hover { background: #00cc00; }
        .btn:disabled { background: #005500; color: #333; cursor: not-allowed; }
        #scan-result { margin-top: 12px; padding: 12px; background: #0f1f0f;
                       border: 1px solid #1a3a1a; border-radius: 4px; font-size: 0.8rem;
                       min-height: 40px; color: #888; }
        table { width: 100%; border-collapse: collapse; font-size: 0.8rem; margin-top: 8px; }
        th { text-align: left; padding: 8px 10px; border-bottom: 1px solid #1a3a1a;
             color: #00aa00; font-weight: normal; text-transform: uppercase; font-size: 0.7rem; }
        td { padding: 8px 10px; border-bottom: 1px solid #111; }
        tr:hover td { background: #0f1f0f; }
        .ev-high { color: #ff6600; font-weight: bold; }
        .ev-ok   { color: #00ff00; }
        .status-pending  { color: #f59e0b; }
        .status-settled  { color: #6b7280; }
        .empty { color: #444; padding: 20px; text-align: center; }
        .loading { color: #555; animation: blink 1s infinite; }
        @keyframes blink { 0%,100%{opacity:1} 50%{opacity:0.3} }
        .tag { display: inline-block; padding: 2px 8px; border-radius: 3px;
               font-size: 0.7rem; background: #0f2f0f; border: 1px solid #1a3a1a; }
    </style>
</head>
<body>
<div class="container">
    <h1>🦅 PREDATOR PAIM v2.0
        <span style="font-size:10px;float:right;color:#444;line-height:2.5">PHD MIT QUANT SYSTEM</span>
    </h1>

    <div class="stats">
        <div class="card">STATUT: <span id="sys-status">ACTIF</span></div>
        <div class="card">CAPITAL: <span>10,000 €</span></div>
        <div class="card">EV+ MIN: <span>8%</span> | SNR MIN: <span>1.5</span></div>
    </div>

    <button class="btn" id="scan-btn" onclick="triggerScan()">⚡ DÉCLENCHER SCAN MANUEL</button>
    <div id="scan-result">Prêt.</div>

    <h3>📊 DERNIERS SIGNAUX — SUPABASE</h3>
    <div id="signals-container">
        <div class="loading">⏳ Chargement des signaux...</div>
    </div>

    <h3>📈 PERFORMANCE</h3>
    <div id="perf-container">
        <div class="loading">⏳ Chargement des métriques...</div>
    </div>
</div>

<script>
async function loadSignals() {
    try {
        const r = await fetch('/api/signals');
        const data = await r.json();
        const container = document.getElementById('signals-container');

        if (!data.signals || data.signals.length === 0) {
            container.innerHTML = '<div class="empty">Aucun signal enregistré pour le moment.</div>';
            return;
        }

        let html = '<table><thead><tr>' +
            '<th>#</th><th>Événement</th><th>Sport</th><th>Sélection</th>' +
            '<th>Bookmaker</th><th>EV+</th><th>Mise</th><th>Statut</th>' +
            '</tr></thead><tbody>';

        data.signals.forEach((s, i) => {
            const ev = parseFloat(s.ev_plus || 0);
            const evClass = ev >= 0.15 ? 'ev-high' : 'ev-ok';
            const evStr = (ev * 100).toFixed(1) + '%';
            const statusClass = s.status === 'settled' ? 'status-settled' : 'status-pending';
            html += `<tr>
                <td>${i + 1}</td>
                <td>${s.event_name || '—'}</td>
                <td><span class="tag">${s.sport || '—'}</span></td>
                <td><b>${s.selection || '—'}</b></td>
                <td>${s.bookmaker_target || '—'}</td>
                <td class="${evClass}">${evStr}</td>
                <td>${s.recommended_stake || '—'}€</td>
                <td class="${statusClass}">${s.status || '—'}</td>
            </tr>`;
        });

        html += '</tbody></table>';
        container.innerHTML = html;
    } catch(e) {
        document.getElementById('signals-container').innerHTML =
            '<div class="empty">Erreur chargement signaux: ' + e.message + '</div>';
    }
}

async function loadPerf() {
    try {
        const r = await fetch('/api/performance');
        const d = await r.json();
        const profitColor = d.total_profit >= 0 ? '#00ff00' : '#ef4444';
        document.getElementById('perf-container').innerHTML = `
            <div class="stats" style="margin-top:8px">
                <div class="card">PARIS: <span>${d.total_bets || 0}</span></div>
                <div class="card">WIN RATE: <span>${((d.win_rate||0)*100).toFixed(1)}%</span></div>
                <div class="card">PROFIT: <span style="color:${profitColor}">${d.total_profit >= 0 ? '+' : ''}${(d.total_profit||0).toFixed(0)}€</span></div>
            </div>`;
    } catch(e) {
        document.getElementById('perf-container').innerHTML =
            '<div class="empty">Erreur chargement métriques.</div>';
    }
}

async function triggerScan() {
    const btn = document.getElementById('scan-btn');
    const result = document.getElementById('scan-result');
    btn.disabled = true;
    btn.textContent = '⏳ Scan en cours...';
    result.style.color = '#888';
    result.textContent = 'Pipeline PAIM en cours d\'exécution...';
    try {
        const r = await fetch('/api/scan');
        const data = await r.json();
        result.style.color = data.status === 'success' ? '#00ff00' : '#ef4444';
        result.textContent = JSON.stringify(data, null, 2);
        await loadSignals();
        await loadPerf();
    } catch(e) {
        result.style.color = '#ef4444';
        result.textContent = '❌ Erreur: ' + e.message;
    } finally {
        btn.disabled = false;
        btn.textContent = '⚡ DÉCLENCHER SCAN MANUEL';
    }
}

// Load on page open
loadSignals();
loadPerf();
</script>
</body>
</html>"""


# ── Routes ────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def root() -> HTMLResponse:
    supabase_url = _get("SUPABASE_URL")
    supabase_key = _get("SUPABASE_KEY")
    
    signals = []
    timestamps = []
    balances = []
    
    if supabase_url and supabase_key:
        try:
            from supabase import create_client
            db = create_client(supabase_url, supabase_key)
            signals = db.table("signals").select("*").order("created_at", desc=True).limit(10).execute().data
            snapshots = db.table("bankroll_snapshots").select("*").order("timestamp").execute().data
            timestamps = [s["timestamp"] for s in snapshots]
            balances = [s["balance"] for s in snapshots]
        except Exception as e:
            logger.error(f"Erreur UI: {e}")

    from jinja2 import Template
    return HTMLResponse(content=Template(HTML_TEMPLATE).render(signals=signals, timestamps=timestamps, balances=balances))


@app.get("/api/health")
async def health() -> JSONResponse:
    return JSONResponse({"status": "ok", "version": "2.0.0"})


@app.get("/api/signals")
async def get_signals() -> JSONResponse:
    """Retourne les derniers signaux depuis Supabase."""
    supabase_url = _get("SUPABASE_URL")
    supabase_key = _get("SUPABASE_KEY")
    if not supabase_url or not supabase_key:
        return JSONResponse({"signals": []})
    try:
        from supabase import create_client
        db = create_client(supabase_url, supabase_key)
        rows = db.table("signals").select("*").order("created_at", desc=True).limit(20).execute()
        return JSONResponse({"signals": rows.data or []})
    except Exception as e:
        logger.error(f"Supabase signals error: {e}")
        return JSONResponse({"signals": [], "error": str(e)})


@app.get("/api/performance")
async def get_performance() -> JSONResponse:
    """Retourne les métriques de performance depuis Supabase."""
    supabase_url = _get("SUPABASE_URL")
    supabase_key = _get("SUPABASE_KEY")
    if not supabase_url or not supabase_key:
        return JSONResponse({"total_bets": 0, "win_rate": 0, "total_profit": 0})
    try:
        from supabase import create_client
        db = create_client(supabase_url, supabase_key)
        rows = db.table("signals").select("*").eq("status", "settled").execute().data or []
        total = len(rows)
        wins = sum(1 for r in rows if r.get("outcome") == 1)
        profit = sum(r.get("profit_eur", 0) or 0 for r in rows)
        return JSONResponse({
            "total_bets": total,
            "win_rate": wins / total if total else 0,
            "total_profit": round(profit, 2),
        })
    except Exception as e:
        logger.error(f"Supabase perf error: {e}")
        return JSONResponse({"total_bets": 0, "win_rate": 0, "total_profit": 0})


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
            "top_signals": [{"event": s["event_name"], "selection": s["selection"],
                             "ev": f"{s['ev_plus']:.1%}", "stake": f"{s['stake']:.0f}€"} for s in top],
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
