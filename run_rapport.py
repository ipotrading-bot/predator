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

from core.constants import ELITE_EDGE as _ELITE_EDGE, kelly_stake as _kelly_stake
from core.db import get_db
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

BANKROLL       = 1000   # Bankroll de référence pour les mises Kelly
SPORT_EMOJI    = {"soccer": "⚽", "basketball": "🏀", "tennis": "🎾"}
SPORT_ORDER    = ["soccer", "basketball", "hockey", "baseball", "rugbyleague", "aussierules"]
ELITE_EDGE     = _ELITE_EDGE
SCAN_STALE_H   = 2      # Alerte si aucun scan depuis X heures
WC_ALPHA_MIN   = 2.0    # Seuil Edge spécifique WC (vs 2.5% général)
_WC_KEYWORDS   = ["world cup", "fifa", "wc 2026", "mondial", "coupe du monde"]


def _is_wc(s: dict) -> bool:
    league = (s.get("league") or "").lower()
    return any(kw in league for kw in _WC_KEYWORDS)


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


def _market_melbet(mkt_key: str, sport: str, mkt_label: str) -> str:
    if mkt_key == "h2h" and sport == "soccer":
        return "Asian Handicap → ligne «0»"
    if mkt_key == "h2h":
        return "Résultat → Moneyline (1×2)"
    # Stored keys are directional ("totals_over", "spreads_home", …) — an
    # exact == "totals"/"spreads" match here never fired.
    if mkt_key.startswith("totals"):
        return f"Total → {mkt_label}"
    if mkt_key.startswith("spreads"):
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
    # kelly_stake defaults to bankroll=BANKROLL_REF (150€) and sport="soccer" —
    # omitting these printed stakes ~6.7× smaller than the "/1000€" the
    # message claims, with the wrong per-sport Kelly fraction on top.
    stake     = _kelly_stake(xbet_odd, prob, BANKROLL, sport)
    if stake == 0:
        return None   # Below MIN_STAKE — skip this signal
    risk      = s.get("risk_flag", "")
    risk_icon = "🔥" if risk == "HIGH_VALUE" else ("✅" if risk == "VALUE" else "📌")
    xbet_mkt  = _market_melbet(mkt_key, sport, mkt_lbl)
    prob_str  = f" | Prob {int(prob*100)}%" if prob > 0 else " | Prob N/A"

    line  = f"{risk_icon} *{team.upper()}*  `{mkt_lbl} @ {xbet_odd:.2f}`\n"
    line += f"   Edge: `+{edge:.1f}%`{prob_str} | Mise: `{stake}€` /1000€\n"
    line += f"   ① 1XBet → {xbet_mkt} → *{team}*\n"
    if pin_odd:
        line += f"   ② Prix fair Pinnacle: `{pin_odd:.2f}`\n"
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

    # ── WC block (top 3 WC signals) — inséré avant les stats générales ─
    wc_signals = [s for s in signals if _is_wc(s)]
    wc_block   = ""
    if wc_signals:
        wc_top = sorted(wc_signals, key=lambda x: x.get("edge_pct", 0), reverse=True)[:3]
        wc_block = "🏆 *FIFA WORLD CUP 2026 — ALPHA DU JOUR*\n"
        for s in wc_top:
            team     = s.get("selection_name") or s.get("match", "?")
            if " vs " in team:
                team = team.split(" vs ")[0].strip()
            edge     = s.get("edge_pct", 0)
            odd      = s.get("xbet_odd", 0)
            prob     = s.get("sharp_prob", 0)
            stake    = _kelly_stake(odd, prob, BANKROLL, s.get("sport", "soccer"))
            is_trap  = edge >= WC_ALPHA_MIN and odd >= 2.20
            icon     = "🔥 TRAP" if is_trap else ("🟢" if edge >= 3.0 else "🟡")
            league   = s.get("league", "")
            wc_block += f"{icon} *{team.upper()}*  `AH 0.0 @ {odd:.2f}`\n"
            wc_block += f"   Edge `+{edge:.1f}%` | Mise `{stake}€` /1000€\n"
            if league:
                wc_block += f"   _({league})_\n"
        wc_block += "\n"

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

    footer = (
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💡 _Mises Kelly fractionnel (×0.20–0.30 selon sport) sur base 1000€_\n"
        f"🔥 Elite | ✅ Value | 📌 Low Value"
    )

    # Telegram limite un message à 4096 chars. Chaque bloc sport ci-dessous
    # est une unité *entités-balancées* (les `*gras*`/`` `code` `` s'ouvrent
    # ET se ferment à l'intérieur du même bloc) — on tronque donc entre deux
    # blocs complets, jamais au milieu d'une chaîne, pour ne pas couper une
    # entité en deux et faire apparaître des astérisques/underscores littéraux
    # au lieu du gras/italique attendu (bug corrigé : l'ancienne version
    # faisait `msg[:3970]` en aveugle, sans savoir où tombait la coupe).
    budget = 4000 - len(header) - len(wc_block) - len(summary) - len(footer) - 80
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
            line = _signal_line(s)
            if line:
                block += line + "\n"
        if len(body) + len(block) > budget:
            truncated = True
            break
        body += block

    if truncated:
        body += "…_(liste tronquée — voir le dashboard pour tous les signaux)_\n\n"

    msg = header + wc_block + summary + body + footer

    _send(msg)
    log.info("Rapport envoyé — %d signaux, %d élite", len(signals), len(elite))
    log.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")


if __name__ == "__main__":
    run()
