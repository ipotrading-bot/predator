"""
api/scan.py — Predator PAIM v2.0 — Main entry point
4-page dashboard + PAIM scan pipeline
"""
from __future__ import annotations
from http.server import BaseHTTPRequestHandler
import json, os, time, logging
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger("predator")

def _get(k): return os.environ.get(k, "")

# ── 4-Page Dashboard HTML ─────────────────────────────────────────

HTML = """<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PREDATOR PAIM v2.0</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#050a05;color:#00ff00;font-family:'Courier New',monospace;min-height:100vh}
nav{background:#0a0a0a;border-bottom:1px solid #0f2f0f;padding:12px 24px;display:flex;align-items:center;gap:24px;position:sticky;top:0;z-index:100}
.logo{font-size:1.1rem;font-weight:bold;color:#00ff00;margin-right:auto}
.logo span{color:#444;font-size:0.7rem;margin-left:8px}
.nav-btn{background:none;border:1px solid #1a3a1a;color:#00aa00;padding:6px 14px;border-radius:3px;cursor:pointer;font-family:'Courier New',monospace;font-size:0.8rem;transition:all .2s}
.nav-btn:hover,.nav-btn.active{background:#0f2f0f;border-color:#00ff00;color:#00ff00}
.page{display:none;padding:24px;max-width:1100px;margin:auto}
.page.active{display:block}
h2{font-size:1.1rem;margin-bottom:20px;border-bottom:1px solid #0f2f0f;padding-bottom:10px;color:#00cc00}
.grid3{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin-bottom:24px}
.grid2{display:grid;grid-template-columns:repeat(2,1fr);gap:16px;margin-bottom:24px}
.card{background:#0a0a0a;border:1px solid #0f2f0f;border-radius:4px;padding:16px}
.card-label{font-size:0.65rem;color:#444;text-transform:uppercase;letter-spacing:.1em;margin-bottom:6px}
.card-value{font-size:1.6rem;font-weight:bold;color:#00ff00}
.card-value.red{color:#ef4444}
.card-value.yellow{color:#f59e0b}
.btn{background:#00ff00;color:#000;border:none;padding:10px 20px;border-radius:3px;font-weight:bold;cursor:pointer;font-family:'Courier New',monospace;font-size:0.85rem}
.btn:hover{background:#00cc00}
.btn:disabled{background:#0a2a0a;color:#1a4a1a;cursor:not-allowed}
.btn-sm{padding:6px 12px;font-size:0.75rem}
#scan-log{background:#0a0a0a;border:1px solid #0f2f0f;border-radius:4px;padding:12px;font-size:0.75rem;min-height:50px;color:#00aa00;white-space:pre-wrap;margin-top:12px}
table{width:100%;border-collapse:collapse;font-size:0.78rem}
th{text-align:left;padding:8px 10px;border-bottom:1px solid #0f2f0f;color:#006600;font-weight:normal;text-transform:uppercase;font-size:0.65rem}
td{padding:8px 10px;border-bottom:1px solid #050a05}
tr:hover td{background:#0a0a0a}
.ev-hot{color:#ff6600;font-weight:bold}
.ev-ok{color:#00ff00}
.badge{display:inline-block;padding:2px 8px;border-radius:2px;font-size:0.65rem;background:#0a1a0a;border:1px solid #0f2f0f}
.badge-pending{color:#f59e0b;border-color:#3a2a00}
.badge-settled{color:#444;border-color:#1a1a1a}
.win{color:#00ff00}
.loss{color:#ef4444}
.countdown{font-size:2rem;font-weight:bold;color:#00ff00;letter-spacing:.1em}
.signal-card{background:#0a0a0a;border:1px solid #0f2f0f;border-radius:4px;padding:14px;margin-bottom:10px}
.signal-card:hover{border-color:#00ff00}
.signal-num{color:#444;font-size:0.7rem;margin-bottom:4px}
.signal-event{font-size:0.95rem;font-weight:bold;margin-bottom:8px}
.signal-meta{display:flex;gap:12px;flex-wrap:wrap;font-size:0.75rem}
.signal-meta span{color:#006600}
.signal-meta b{color:#00ff00}
.chart-wrap{background:#0a0a0a;border:1px solid #0f2f0f;border-radius:4px;padding:16px;height:220px;position:relative}
canvas{width:100%!important;height:180px!important}
.empty{color:#1a3a1a;text-align:center;padding:40px;font-size:0.85rem}
.status-dot{width:8px;height:8px;border-radius:50%;background:#00ff00;display:inline-block;margin-right:6px;animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
</style>
</head>
<body>
<nav>
  <div class="logo">🦅 PREDATOR PAIM <span>v2.0 | PHD MIT QUANT</span></div>
  <button class="nav-btn active" onclick="showPage('terminal')">TERMINAL</button>
  <button class="nav-btn" onclick="showPage('signals')">SIGNALS 7/9</button>
  <button class="nav-btn" onclick="showPage('ledger')">LEDGER</button>
  <button class="nav-btn" onclick="showPage('audit')">AUDIT</button>
</nav>

<!-- PAGE 1: TERMINAL -->
<div id="page-terminal" class="page active">
  <h2><span class="status-dot"></span>TERMINAL DE CONTRÔLE</h2>
  <div class="grid3">
    <div class="card"><div class="card-label">Statut Système</div><div class="card-value" id="sys-state">ACTIF</div></div>
    <div class="card"><div class="card-label">Capital Initial</div><div class="card-value">10,000 €</div></div>
    <div class="card"><div class="card-label">Prochain Scan</div><div class="countdown" id="countdown">--:--:--</div></div>
  </div>
  <div class="grid3">
    <div class="card"><div class="card-label">EV+ Minimum</div><div class="card-value">8%</div></div>
    <div class="card"><div class="card-label">Kelly Fraction</div><div class="card-value">0.25×</div></div>
    <div class="card"><div class="card-label">Kill-Switch</div><div class="card-value yellow">15% DD</div></div>
  </div>
  <button class="btn" id="scan-btn" onclick="forceScan()">⚡ FORCE SCAN MANUEL</button>
  <div id="scan-log">Système prêt. En attente du prochain cycle...</div>
</div>

<!-- PAGE 2: SIGNALS -->
<div id="page-signals" class="page">
  <h2>TICKET SYSTÈME 7/9 — CYCLE ACTUEL</h2>
  <div id="signals-list"><div class="empty">Chargement des signaux...</div></div>
</div>

<!-- PAGE 3: LEDGER -->
<div id="page-ledger" class="page">
  <h2>LEDGER — HISTORIQUE COMPLET</h2>
  <div id="ledger-table"><div class="empty">Chargement...</div></div>
</div>

<!-- PAGE 4: AUDIT -->
<div id="page-audit" class="page">
  <h2>AUDIT QUANTITATIF</h2>
  <div class="grid3" id="audit-kpis">
    <div class="card"><div class="card-label">Total Paris</div><div class="card-value" id="a-total">—</div></div>
    <div class="card"><div class="card-label">Win Rate</div><div class="card-value" id="a-wr">—</div></div>
    <div class="card"><div class="card-label">Profit Net</div><div class="card-value" id="a-profit">—</div></div>
  </div>
  <div class="grid2">
    <div class="card"><div class="card-label">CLV Index (cible &gt;5%)</div><div class="card-value" id="a-clv">—</div></div>
    <div class="card"><div class="card-label">Brier Score (cible &lt;0.25)</div><div class="card-value" id="a-brier">—</div></div>
  </div>
  <div class="chart-wrap">
    <div class="card-label" style="margin-bottom:8px">EQUITY CURVE — Bankroll (€)</div>
    <canvas id="equity-chart"></canvas>
  </div>
</div>

<script>
// ── Navigation ────────────────────────────────────────────────
function showPage(name) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('page-' + name).classList.add('active');
  event.target.classList.add('active');
  if (name === 'signals') loadSignals();
  if (name === 'ledger') loadLedger();
  if (name === 'audit') loadAudit();
}

// ── Countdown ─────────────────────────────────────────────────
let nextScanTs = 0;
function updateCountdown() {
  if (!nextScanTs) return;
  const diff = nextScanTs - Math.floor(Date.now() / 1000);
  if (diff <= 0) { document.getElementById('countdown').textContent = 'SCAN...'; return; }
  const h = String(Math.floor(diff / 3600)).padStart(2,'0');
  const m = String(Math.floor((diff % 3600) / 60)).padStart(2,'0');
  const s = String(diff % 60).padStart(2,'0');
  document.getElementById('countdown').textContent = h + ':' + m + ':' + s;
}
setInterval(updateCountdown, 1000);

// ── Force Scan ────────────────────────────────────────────────
async function forceScan() {
  const btn = document.getElementById('scan-btn');
  const log = document.getElementById('scan-log');
  btn.disabled = true; btn.textContent = '⏳ Scan en cours...';
  log.textContent = '[' + new Date().toISOString() + '] Pipeline PAIM démarré...\n';
  try {
    const r = await fetch('/api/scan', {method:'POST'});
    const d = await r.json();
    log.textContent += JSON.stringify(d, null, 2);
    if (d.status === 'success') {
      log.textContent += '\n\n✅ ' + (d.message || 'Scan terminé.');
      loadSignals();
    }
  } catch(e) { log.textContent += '\n❌ Erreur: ' + e.message; }
  finally { btn.disabled = false; btn.textContent = '⚡ FORCE SCAN MANUEL'; }
}

// ── Signals Page ──────────────────────────────────────────────
async function loadSignals() {
  const el = document.getElementById('signals-list');
  try {
    const r = await fetch('/api/signals');
    const d = await r.json();
    nextScanTs = d.next_scan_ts || 0;
    const sigs = (d.signals || []).filter(s => s.status === 'pending').slice(0, 9);
    if (!sigs.length) { el.innerHTML = '<div class="empty">Aucun signal actif. Déclenchez un scan.</div>'; return; }
    el.innerHTML = sigs.map((s, i) => {
      const ev = parseFloat(s.ev_plus || 0);
      const evStr = (ev * 100).toFixed(1) + '%';
      const evClass = ev >= 0.15 ? 'ev-hot' : 'ev-ok';
      const icon = ev >= 0.15 ? '🔥' : '✅';
      return `<div class="signal-card">
        <div class="signal-num">${icon} SÉLECTION #${i+1} / 9</div>
        <div class="signal-event">${s.event_name || '—'}</div>
        <div class="signal-meta">
          <span>Sport: <b>${s.sport || '—'}</b></span>
          <span>Sélection: <b>${s.selection || '—'}</b></span>
          <span>Bookmaker: <b>${s.bookmaker_target || '—'}</b></span>
          <span>EV+: <b class="${evClass}">${evStr}</b></span>
          <span>Prob. Sharp: <b>${((s.sharp_prob||0)*100).toFixed(1)}%</b></span>
          <span>Mise: <b>${s.recommended_stake || '—'}€</b></span>
        </div>
      </div>`;
    }).join('');
  } catch(e) { el.innerHTML = '<div class="empty">Erreur: ' + e.message + '</div>'; }
}

// ── Ledger Page ───────────────────────────────────────────────
async function loadLedger() {
  const el = document.getElementById('ledger-table');
  try {
    const r = await fetch('/api/signals');
    const d = await r.json();
    const rows = d.signals || [];
    if (!rows.length) { el.innerHTML = '<div class="empty">Aucun historique disponible.</div>'; return; }
    let html = `<table><thead><tr>
      <th>Événement</th><th>Sport</th><th>Sélection</th><th>EV+</th>
      <th>Mise</th><th>Résultat</th><th>Profit</th><th>Statut</th>
    </tr></thead><tbody>`;
    rows.forEach(s => {
      const ev = ((s.ev_plus||0)*100).toFixed(1) + '%';
      const evClass = parseFloat(s.ev_plus||0) >= 0.15 ? 'ev-hot' : 'ev-ok';
      const outcome = s.outcome === 1 ? '<span class="win">✅ WIN</span>'
                    : s.outcome === 0 ? '<span class="loss">❌ LOSS</span>' : '—';
      const profit = s.profit_eur != null
        ? `<span class="${s.profit_eur >= 0 ? 'win' : 'loss'}">${s.profit_eur >= 0 ? '+' : ''}${s.profit_eur}€</span>`
        : '—';
      const badge = s.status === 'settled'
        ? '<span class="badge badge-settled">settled</span>'
        : '<span class="badge badge-pending">pending</span>';
      html += `<tr>
        <td>${s.event_name || '—'}</td>
        <td>${s.sport || '—'}</td>
        <td><b>${s.selection || '—'}</b></td>
        <td class="${evClass}">${ev}</td>
        <td>${s.recommended_stake || '—'}€</td>
        <td>${outcome}</td>
        <td>${profit}</td>
        <td>${badge}</td>
      </tr>`;
    });
    html += '</tbody></table>';
    el.innerHTML = html;
  } catch(e) { el.innerHTML = '<div class="empty">Erreur: ' + e.message + '</div>'; }
}

// ── Audit Page ────────────────────────────────────────────────
let chartInstance = null;
async function loadAudit() {
  try {
    const r = await fetch('/api/audit');
    const d = await r.json();
    document.getElementById('a-total').textContent = d.total_bets || 0;
    const wr = ((d.win_rate||0)*100).toFixed(1) + '%';
    document.getElementById('a-wr').textContent = wr;
    document.getElementById('a-wr').className = 'card-value ' + (d.win_rate >= 0.55 ? '' : 'yellow');
    const profit = d.total_profit || 0;
    document.getElementById('a-profit').textContent = (profit >= 0 ? '+' : '') + profit.toFixed(0) + '€';
    document.getElementById('a-profit').className = 'card-value ' + (profit >= 0 ? '' : 'red');
    document.getElementById('a-clv').textContent = ((d.clv_avg||0)*100).toFixed(2) + '%';
    document.getElementById('a-brier').textContent = d.brier_score != null ? d.brier_score.toFixed(4) : 'N/A';

    // Equity curve
    const curve = d.equity_curve || [];
    if (curve.length > 1) {
      const labels = curve.map(p => p.timestamp ? p.timestamp.substring(0,10) : '');
      const values = curve.map(p => p.balance);
      const ctx = document.getElementById('equity-chart').getContext('2d');
      if (chartInstance) chartInstance.destroy();
      chartInstance = new Chart(ctx, {
        type: 'line',
        data: {
          labels,
          datasets: [{
            data: values,
            borderColor: '#00ff00',
            backgroundColor: 'rgba(0,255,0,0.05)',
            borderWidth: 2,
            pointRadius: 0,
            fill: true,
            tension: 0.3,
          }]
        },
        options: {
          responsive: true,
          plugins: { legend: { display: false } },
          scales: {
            x: { ticks: { color: '#006600', font: { size: 9 } }, grid: { color: '#0a1a0a' } },
            y: { ticks: { color: '#006600', font: { size: 9 }, callback: v => v + '€' }, grid: { color: '#0a1a0a' } }
          }
        }
      });
    } else {
      document.getElementById('equity-chart').parentElement.innerHTML +=
        '<div class="empty" style="margin-top:-160px">Données insuffisantes pour la courbe.</div>';
    }
  } catch(e) { console.error(e); }
}

// ── Init ──────────────────────────────────────────────────────
(async () => {
  try {
    const r = await fetch('/api/signals');
    const d = await r.json();
    nextScanTs = d.next_scan_ts || 0;
  } catch(e) {}
})();
</script>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
</body>
</html>"""


# ── PAIM Engine (inline) ──────────────────────────────────────────

def _shin_probs(odds):
    total = sum(1/o for o in odds)
    return [1/(o*total) for o in odds]

def _compute_ev(prob, odds):
    return prob*(odds-1)-(1-prob)

def _kelly_stake(prob, odds, bankroll=10000.0, fraction=0.25):
    b = odds-1
    f = max((b*prob-(1-prob))/b, 0.0)
    return round(f*fraction*bankroll/10)*10

def _process_event(event):
    sharp = next((b for b in event.get("bookmakers",[]) if b["key"]=="pinnacle"), None)
    soft  = next((b for b in event.get("bookmakers",[]) if b["key"] in ("bet365","unibet","1xbet")), None)
    if not sharp or not soft: return None
    sm = next((m for m in sharp.get("markets",[]) if m["key"]=="h2h"), None)
    fm = next((m for m in soft.get("markets",[])  if m["key"]=="h2h"), None)
    if not sm or not fm: return None
    so, fo = sm.get("outcomes",[]), fm.get("outcomes",[])
    if len(so)<2 or len(fo)<2: return None
    probs = _shin_probs([o["price"] for o in so[:2]])
    best_ev, best_sel, best_odds, best_prob = -999.0,"",0.0,0.0
    for i,outcome in enumerate(fo[:2]):
        if i>=len(probs): break
        ev = _compute_ev(probs[i], outcome["price"])
        if ev>best_ev:
            best_ev,best_sel,best_odds,best_prob = ev,outcome["name"],outcome["price"],probs[i]
    if best_ev<0.08: return None
    return {
        "event_id": event.get("id",""),
        "event_name": f"{event.get('home_team')} vs {event.get('away_team')}",
        "sport": event.get("sport_key",""),
        "selection": best_sel,
        "bookmaker": soft["key"],
        "ev_plus": best_ev,
        "sharp_prob": best_prob,
        "stake": _kelly_stake(best_prob, best_odds),
    }

async def _run_pipeline():
    import httpx
    odds_key = _get("ODDS_API_KEY")
    if not odds_key:
        return {"status":"error","message":"ODDS_API_KEY manquant"}

    sports = ["soccer_epl","soccer_ligue_1","basketball_nba","americanfootball_nfl","tennis_atp"]
    all_events = []
    start = time.monotonic()

    async with httpx.AsyncClient(timeout=15.0) as client:
        for sport in sports:
            try:
                r = await client.get(
                    f"https://api.the-odds-api.com/v4/sports/{sport}/odds/",
                    params={"apiKey":odds_key,"regions":"eu","markets":"h2h",
                            "oddsFormat":"decimal","bookmakers":"pinnacle,bet365,unibet,1xbet"},
                )
                if r.status_code==200: all_events.extend(r.json())
            except Exception as e:
                logger.warning(f"Fetch {sport}: {e}")

    signals = [s for e in all_events if (s:=_process_event(e))]
    signals.sort(key=lambda x: x["ev_plus"], reverse=True)
    top = signals[:9]

    # Persist
    url,key = _get("SUPABASE_URL"),_get("SUPABASE_KEY")
    if url and key and top:
        try:
            from supabase import create_client
            db = create_client(url,key)
            for s in top:
                db.table("signals").insert({
                    "event_id": s["event_id"],
                    "event_name": s["event_name"],
                    "sport": s["sport"],
                    "selection": s["selection"],
                    "bookmaker_target": s["bookmaker"],
                    "ev_plus": round(s["ev_plus"],5),
                    "sharp_prob": round(s["sharp_prob"],5),
                    "recommended_stake": s["stake"],
                    "status": "pending",
                    "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),
                }).execute()
        except Exception as e:
            logger.error(f"Supabase: {e}")

    # Telegram
    tg_token,tg_chat = _get("TELEGRAM_BOT_TOKEN"),_get("TELEGRAM_CHAT_ID")
    if tg_token and tg_chat and top:
        try:
            import httpx as _hx
            lines = ["*PREDATOR PAIM - TICKET SYSTEME 7/9*","="*28]
            for i,s in enumerate(top,1):
                icon = "🔥" if s["ev_plus"]>=0.15 else "✅"
                lines.append(f"{icon} #{i} {s['event_name']}\n   -> {s['selection']} | EV {s['ev_plus']:.1%} | {s['stake']:.0f}EUR")
            lines.append(f"\nSysteme 7/{len(top)} - Profit des 7 bons resultats")
            async with _hx.AsyncClient() as c:
                await c.post(f"https://api.telegram.org/bot{tg_token}/sendMessage",
                    json={"chat_id":tg_chat,"text":"\n".join(lines),"parse_mode":"Markdown"})
        except Exception as e:
            logger.error(f"Telegram: {e}")

    duration = round(time.monotonic()-start,2)
    if not top:
        return {"status":"success","message":"Aucune anomalie EV+ detectee.",
                "events_analyzed":len(all_events),"duration_seconds":duration}
    return {"status":"success","message":f"Ticket Elite envoye - {len(top)} signaux.",
            "events_analyzed":len(all_events),"signals_validated":len(top),
            "top_signals":[{"event":s["event_name"],"ev":f"{s['ev_plus']:.1%}","stake":f"{s['stake']:.0f}EUR"} for s in top],
            "duration_seconds":duration}


# ── HTTP Handler ──────────────────────────────────────────────────

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", ""):
            body = HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/scan":
            self._run_scan()
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if urlparse(self.path).path == "/api/scan":
            self._run_scan()
        else:
            self.send_response(404)
            self.end_headers()

    def _run_scan(self):
        import asyncio
        try:
            result = asyncio.run(_run_pipeline())
        except Exception as e:
            result = {"status":"error","message":str(e)}
        body = json.dumps(result).encode()
        self.send_response(200)
        self.send_header("Content-Type","application/json")
        self.send_header("Access-Control-Allow-Origin","*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass
