"""
run_rapport.py — PREDATOR PAIM v8.5 — Rapport Telegram 06:05 & 18:05 UTC
Triggered by .github/workflows/rapport.yml after each main scan window.

Envoie un récapitulatif des signaux actifs, un bloc par pari :
  - Événement, favori (h2h uniquement), sélection, cote, heure du match, valeur
  - Aucune mise ni bankroll — supprimées sur demande opérateur (2026-07-21)
  - Alerte erreur si aucun scan récent ou Supabase KO
"""
import logging
import os
import time
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

from core.constants import ELITE_EDGE as _ELITE_EDGE
from core.db import get_db
from core.paim_engine import resolve_selection_side as _resolve_side
from core.learning_layer import load_learning_summary as _load_learning_summary
from core.stats_utils import p_breakeven as _p_breakeven, wilson_ci as _wilson_ci

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

# Aligné sur run_engine.SPORT_EMOJI — n'en couvrir que 3 renvoyait le 🎯
# générique sur la majorité des signaux (baseball, hockey, WNBA…).
SPORT_EMOJI    = {
    "soccer": "⚽", "tennis": "🎾", "basketball": "🏀", "boxing": "🥊",
    "mma": "🥋", "darts": "🎯", "cricket": "🏏", "hockey": "🏒",
    "esports": "🎮", "americanfootball": "🏈", "baseball": "⚾",
    "rugby": "🏉", "rugbyleague": "🏉", "aussierules": "🦘",
    "volleyball": "🏐", "tabletennis": "🏓", "handball": "🤾",
}
SPORT_ORDER    = ["soccer", "basketball", "hockey", "baseball", "rugbyleague", "aussierules"]
ELITE_EDGE     = _ELITE_EDGE
SCAN_STALE_H   = 2      # Alerte si aucun scan depuis X heures

# Fenêtre de signaux couverte par un rapport. Doit rester ALIGNÉE sur le cron
# de rapport.yml (toutes les 2h depuis le 2026-08-06) : à 6h, chaque signal
# était réenvoyé par trois rapports consécutifs et l'opérateur ne pouvait plus
# distinguer une nouvelle occasion d'un rappel. Si le cron change, changer ici.
REPORT_WINDOW_H = 2

# (Retiré 2026-08-06 — Coupe du Monde terminée, instruction opérateur : le bloc
# « 🏆 COUPE DU MONDE 2026 » du rapport, son seuil d'edge dédié à 2,0% et la
# détection par mots-clés de ligue sont partis avec.)


def _send(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        log.warning("Telegram non configuré — message non envoyé")
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT, "text": text,
                  "parse_mode": "Markdown", "disable_web_page_preview": True},
            timeout=15,
        )
        if r.status_code != 200:
            log.error("Telegram HTTP %d: %s", r.status_code, r.text[:300])
        else:
            log.info("Telegram envoyé (%d chars)", len(text))
    except Exception as e:
        log.error("Telegram erreur: %s", e)


def _kickoff(s: dict, now: datetime) -> str:
    """' · 21:00 UTC' today, ' · 22/07 21:00 UTC' otherwise, '' if unknown."""
    raw = s.get("match_time") or ""
    if not raw:
        return ""
    try:
        mt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return ""
    if mt.tzinfo is None:
        mt = mt.replace(tzinfo=timezone.utc)
    same_day = mt.date() == now.date()
    return f" · {mt.strftime('%H:%M') if same_day else mt.strftime('%d/%m %H:%M')} UTC"


def _favourite(s: dict) -> str:
    """Team the sharp price makes favourite, '' when not derivable.

    h2h only: sharp_prob is this selection's probability, so >= 50% means the
    pick IS the favourite. Totals/spreads price a line, not a side — no
    moneyline is stored for them, so naming a favourite there would be
    inventing it.
    """
    if s.get("market_key") != "h2h":
        return ""
    prob  = s.get("sharp_prob") or 0
    match = s.get("match") or ""
    sel   = s.get("selection_name") or ""
    if not prob or " vs " not in match:
        return ""
    home, away = (p.strip() for p in match.split(" vs ", 1))
    if prob >= 0.5:
        return sel or home
    is_home = _resolve_side(sel, home, away)
    if is_home is None:
        return ""
    return away if is_home else home


def _signal_line(s: dict, now: datetime) -> str | None:
    """One signal, operator format (2026-07-21): event, favourite, pick, odds,
    kick-off, value. No stake and no bankroll — Kelly sizing was printing
    "Mise 0€ /1000€" on nearly every line, which told the operator nothing and
    silently DROPPED any signal sized below MIN_STAKE from the report."""
    emoji     = SPORT_EMOJI.get(s.get("sport", ""), "🎯")
    match     = s.get("match") or "?"
    sel       = s.get("selection_name") or match
    edge      = s.get("edge_pct", 0)
    xbet_odd  = s.get("xbet_odd", 0)
    risk      = s.get("risk_flag", "")
    risk_icon = "🔥" if risk == "HIGH_VALUE" else ("✅" if risk == "VALUE" else "📌")

    # Le favori n'a sa propre ligne que quand il N'EST PAS le pari : sinon
    # elle répéterait mot pour mot la ligne suivante. Quand le pari EST le
    # favori, un simple "(favori)" inline le dit sans ligne en plus.
    fav = _favourite(s)
    line = f"{emoji} *{match}*{_kickoff(s, now)}\n"
    if fav and fav != sel:
        line += f"   Favori : {fav}\n"
    tag = " (favori)" if fav and fav == sel else ""
    line += f"   {risk_icon} {sel}{tag} `@ {xbet_odd:.2f}` · valeur `+{edge:.1f}%`\n"
    return line


def _performance_block(sb) -> str:
    """
    Recent real performance (outcome-driven, Task 4) — Wilson 95% CI and
    tax-adjusted breakeven probability, never a bare win rate. Pulled from
    ai_learning_ledger (permanent record), not `signals` (purged ~48h).
    """
    try:
        res = (sb.table("ai_learning_ledger")
               .select("outcome, odds")
               .order("created_at", desc=True)
               .limit(200)
               .execute())
        rows = res.data or []
    except Exception as e:
        log.warning("Performance block: %s", e)
        return ""

    decisive = [r for r in rows if r.get("outcome") in ("WIN", "LOSS")]
    if len(decisive) < 10:
        return ""

    wins = sum(1 for r in decisive if r["outcome"] == "WIN")
    lo, hi = _wilson_ci(wins, len(decisive))
    odds_vals = [r["odds"] for r in decisive if r.get("odds")]
    avg_odds = sum(odds_vals) / len(odds_vals) if odds_vals else None
    breakeven = _p_breakeven(avg_odds) if avg_odds else None

    win_rate = wins / len(decisive) * 100
    line = (
        f"📈 *Performance réelle* (derniers {len(decisive)} paris réglés)\n"
        f"   `{win_rate:.1f}%` — IC 95% `[{lo*100:.1f}% – {hi*100:.1f}%]`\n"
    )
    if breakeven is not None:
        status = "✅ confirmé rentable" if lo > breakeven else "⚠️ pas encore confirmé"
        line += f"   Seuil rentable net taxe: `{breakeven*100:.1f}%` — {status}\n"

    # core/learning_layer.py's compute_and_save() persists a plain-language
    # summary of what it changed last audit cycle (threshold moves, edge-band
    # warnings) — surfacing it here means a threshold correction is
    # something the operator actually sees, not just a GitHub Actions log
    # line nobody reads.
    try:
        summary = _load_learning_summary(sb)
    except Exception as e:
        log.warning("Learning summary: %s", e)
        summary = []
    if summary:
        line += "\n🧠 *Learning* (dernier cycle)\n"
        for s in summary[:5]:
            line += f"   • {s}\n"

    return line + "\n"


def run():
    now = datetime.now(timezone.utc)
    log.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    log.info("PREDATOR RAPPORT — %s", now.strftime("%Y-%m-%d %H:%M UTC"))

    # ── Connexion Supabase (lecture seule — clé anon suffit) ───────────
    try:
        sb = get_db(write=False)
        if sb is None:
            raise RuntimeError("SUPABASE_URL/SUPABASE_KEY not configured")
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
        cutoff = (now - timedelta(hours=REPORT_WINDOW_H)).isoformat()
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

    log.info("%d signaux actifs (%d dernières heures)", len(signals), REPORT_WINDOW_H)

    # ── Construction du message ───────────────────────────────────────
    # 12 rapports/jour depuis le passage au cron 2h : « MATIN / SOIR » ne
    # distinguait plus rien, six rapports partageaient le même libellé.
    slot  = ("🌙 NUIT"       if now.hour < 6  else
             "☀️ MATIN"      if now.hour < 12 else
             "🌤 APRÈS-MIDI" if now.hour < 18 else
             "🌆 SOIR")
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
            f"📭 *Aucun signal actif* dans les {REPORT_WINDOW_H} dernières heures.\n\n"
            "_Le moteur scanne toutes les 2h. "
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
    summary += _performance_block(sb)

    # Groupement par sport
    by_sport: dict = {}
    for s in signals:
        sp = s.get("sport", "soccer")
        by_sport.setdefault(sp, []).append(s)

    footer = "━━━━━━━━━━━━━━━━━━━━━━━━━━\n🔥 Élite | ✅ Value | 📌 Faible"

    # Telegram limite un message à 4096 chars. Chaque bloc sport ci-dessous
    # est une unité *entités-balancées* (les `*gras*`/`` `code` `` s'ouvrent
    # ET se ferment à l'intérieur du même bloc) — on tronque donc entre deux
    # blocs complets, jamais au milieu d'une chaîne, pour ne pas couper une
    # entité en deux et faire apparaître des astérisques/underscores littéraux
    # au lieu du gras/italique attendu (bug corrigé : l'ancienne version
    # faisait `msg[:3970]` en aveugle, sans savoir où tombait la coupe).
    budget = 4000 - len(header) - len(summary) - len(footer) - 80
    body = ""
    truncated = False
    ordered_sports = list(SPORT_ORDER) + sorted(s for s in by_sport if s not in SPORT_ORDER)
    for sport in ordered_sports:
        group = by_sport.get(sport, [])
        if not group:
            continue
        emoji = SPORT_EMOJI.get(sport, "🎯")
        block = f"{emoji} *{sport.upper()}* — {len(group)} signal(s)\n"
        for s in group:
            line = _signal_line(s, now)
            if line:
                block += line + "\n"
        if len(body) + len(block) > budget:
            truncated = True
            break
        body += block

    if truncated:
        body += "…_(liste tronquée — voir le dashboard pour tous les signaux)_\n\n"

    msg = header + summary + body + footer

    _send(msg)
    log.info("Rapport envoyé — %d signaux, %d élite", len(signals), len(elite))
    log.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")


if __name__ == "__main__":
    run()
