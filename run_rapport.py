"""
run_rapport.py — PREDATOR PAIM v8.5 — Rapport Telegram 06:05 & 18:05 UTC
Triggered by .github/workflows/rapport.yml after each main scan window.

Envoie un récapitulatif complet des signaux actifs :
  - Équipe à miser, marché, cote, mise Kelly (base 1000€)
  - Alerte erreur si aucun scan récent ou Supabase KO
"""
import logging
import os
import time
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv
from supabase import create_client

from core.constants import (
    ELITE_EDGE as _ELITE_EDGE,
    kelly_stake as _kelly_stake,
    risk_flag as _risk_flag,
)

load_dotenv()

_fmt = logging.Formatter(fmt="%(asctime)s UTC | %(levelname)-7s | %(message)s",
                          datefmt="%Y-%m-%d %H:%M:%S")
_fmt.converter = time.gmtime
_h = logging.StreamHandler()
_h.setFormatter(_fmt)
log = logging.getLogger("RAPPORT")
log.setLevel(logging.INFO)
log.addHandler(_h)
log.propagate = False

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT  = os.environ.get("TELEGRAM_CHAT_ID")
SUPABASE_URL   = os.environ.get("SUPABASE_URL")
SUPABASE_KEY   = os.environ.get("SUPABASE_KEY")

BANKROLL       = 1000   # Bankroll de référence pour les mises Kelly
SPORT_EMOJI    = {"soccer": "⚽", "basketball": "🏀", "tennis": "🎾"}
SPORT_ORDER    = ["basketball", "tennis", "soccer"]
ELITE_EDGE     = _ELITE_EDGE
SCAN_STALE_H   = 2      # Alerte si aucun scan depuis X heures


def _send(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        log.warning("Telegram non configuré — message non envoyé")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT, "text": text,
                  "parse_mode": "Markdown", "disable_web_page_preview": True},
            timeout=15,
        )
        log.info("Telegram envoyé (%d chars)", len(text))
    except Exception as e:
        log.error("Telegram erreur: %s", e)


def _market_1xbet(mkt_key: str, sport: str, mkt_label: str) -> str:
    if mkt_key == "h2h" and sport == "soccer":
        return "Asian Handicap → ligne «0»"
    if mkt_key == "h2h":
        return "Résultat → Moneyline (1×2)"
    if mkt_key == "totals":
        return f"Total → {mkt_label}"
    if mkt_key == "spreads":
        return f"Handicap → {mkt_label}"
    return mkt_label or "Voir Handicap/Total"


def _signal_line(s: dict) -> str | None:
    """Format one signal as a Telegram-friendly string."""
    emoji     = SPORT_EMOJI.get(s.get("sport", ""), "🎯")
    team      = s.get("selection_name") or s.get("match", "?")
    if " vs " in team:                              # old signal — use home team
        team = team.split(" vs ")[0].strip()
    edge      = s.get("edge_pct", 0)
    prob      = s.get("sharp_prob", 0)
    xbet_odd  = s.get("xbet_odd", 0)
    pin_odd   = s.get("pinnacle_price", 0)
    mkt_key   = s.get("market_key", "h2h")
    mkt_lbl   = s.get("market", "AH 0.0")
    sport     = s.get("sport", "soccer")
    stake     = _kelly_stake(xbet_odd, prob)
    if stake == 0:
        return None   # Below MIN_STAKE — skip this signal
    risk      = s.get("risk_flag", "")
    risk_icon = "🔥" if risk == "HIGH_VALUE" else ("✅" if risk == "VALUE" else "📌")
    xbet_mkt  = _market_1xbet(mkt_key, sport, mkt_lbl)
    prob_str  = f" | Prob {int(prob*100)}%" if prob > 0 else " | Prob N/A"

    line  = f"{risk_icon} *{team.upper()}*  `{mkt_lbl} @ {xbet_odd:.2f}`\n"
    line += f"   Edge: `+{edge:.1f}%`{prob_str} | Mise: `{stake}€` /1000€\n"
    line += f"   ① 1XBet → {xbet_mkt} → *{team}*\n"
    if pin_odd:
        line += f"   ② Prix fair Pinnacle: `{pin_odd:.2f}`\n"
    return line


def run():
    now = datetime.now(timezone.utc)
    log.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    log.info("PREDATOR RAPPORT — %s", now.strftime("%Y-%m-%d %H:%M UTC"))

    # ── Connexion Supabase ────────────────────────────────────────────
    try:
        sb = create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as e:
        log.error("Supabase KO: %s", e)
        _send(
            f"⚠️ *PREDATOR ALERTE ERREUR*\n"
            f"Date: `{now.strftime('%d/%m/%Y %H:%M UTC')}`\n"
            f"Erreur: Connexion Supabase échouée\n`{e}`"
        )
        return

    # ── Vérification heartbeat (dernier scan récent ?) ────────────────
    last_scan_at = None
    try:
        res = sb.table("meta").select("value").eq("key", "last_scan").limit(1).execute()
        if res.data:
            import json
            meta = json.loads(res.data[0]["value"])
            last_scan_at = meta.get("at")
    except Exception as e:
        log.warning("Meta heartbeat: %s", e)

    stale = False
    if last_scan_at:
        try:
            from datetime import datetime as _dt
            last_dt = _dt.fromisoformat(last_scan_at.replace("Z", "+00:00"))
            age_h = (now - last_dt).total_seconds() / 3600
            if age_h > SCAN_STALE_H:
                stale = True
                log.warning("Dernier scan il y a %.1fh — moteur potentiellement en erreur", age_h)
        except Exception:
            pass
    else:
        stale = True

    # ── Récupération signaux actifs ───────────────────────────────────
    signals = []
    try:
        cutoff = (now - timedelta(hours=6)).isoformat()
        res = (sb.table("signals")
               .select("*")
               .eq("status", "active")
               .gte("scanned_at", cutoff)
               .order("edge_pct", desc=True)
               .limit(30)
               .execute())
        signals = res.data or []
    except Exception as e:
        log.error("Fetch signaux: %s", e)
        _send(
            f"⚠️ *PREDATOR ALERTE ERREUR*\n"
            f"`{now.strftime('%d/%m/%Y %H:%M UTC')}`\n"
            f"Erreur lecture signaux: `{e}`"
        )
        return

    log.info("%d signaux actifs (6 dernières heures)", len(signals))

    # ── Construction du message ───────────────────────────────────────
    slot  = "☀️ MATIN" if now.hour < 12 else "🌆 SOIR"
    date  = now.strftime("%d/%m/%Y")
    heure = now.strftime("%H:%M")

    elite = [s for s in signals if (s.get("edge_pct") or 0) >= ELITE_EDGE]

    header = (
        f"🎯 *PREDATOR — {slot} {date} {heure} UTC*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    )

    if stale:
        header += f"⚠️ _Moteur inactif depuis +{SCAN_STALE_H}h — vérifiez GitHub Actions_\n\n"

    if not signals:
        msg = (
            header +
            "📭 *Aucun signal actif* dans les 6 dernières heures.\n\n"
            "_Le moteur tourne toutes les 30 min. "
            "Si ce message répète, vérifiez GitHub Actions → Predator Engine._"
        )
        _send(msg)
        log.info("Rapport envoyé — 0 signaux")
        return

    # Stats globales
    best_edge = max((s.get("edge_pct") or 0) for s in signals)
    summary = (
        f"📊 `{len(signals)}` signaux · `{len(elite)}` élite ≥2.5% · "
        f"Meilleur edge: `+{best_edge:.1f}%`\n\n"
    )

    # Groupement par sport
    by_sport: dict = {}
    for s in signals:
        sp = s.get("sport", "soccer")
        by_sport.setdefault(sp, []).append(s)

    body = ""
    for sport in SPORT_ORDER:
        group = by_sport.get(sport, [])
        if not group:
            continue
        emoji = SPORT_EMOJI.get(sport, "🎯")
        body += f"{emoji} *{sport.upper()}* — {len(group)} signal(s)\n"
        for s in group:
            line = _signal_line(s)
            if line:
                body += line + "\n"

    footer = (
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💡 _Mises Kelly ×0.25 sur base 1000€_\n"
        f"🔥 Elite | ✅ Value | 📌 Low Value"
    )

    msg = header + summary + body + footer

    # Telegram a une limite de 4096 chars
    if len(msg) > 4000:
        msg = msg[:3970] + "\n…_(tronqué — voir dashboard)_"

    _send(msg)
    log.info("Rapport envoyé — %d signaux, %d élite", len(signals), len(elite))
    log.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")


if __name__ == "__main__":
    run()
