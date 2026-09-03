"""
run_rapport.py — PREDATOR PAIM — digest Telegram toutes les 2 h (H+35)
Déclenché par .github/workflows/reports.yml (job `rapport`).

Un seul message, une seule règle (2026-09-03) : la liste des paris
RECOMMANDÉS encore jouables — coup d'envoi devant nous, jamais un fantôme.
  - 🆕 les signaux nés depuis le digest précédent (REPORT_WINDOW_H) ;
  - ⏳ ceux déjà annoncés et toujours jouables, en rappel.
  - Événement, favori (h2h uniquement), sélection, cote, heure, valeur.
  - Aucune mise ni bankroll — supprimées sur demande opérateur (2026-07-21).
  - Rien à lister et moteur vivant : SILENCE (le scan standard dit déjà
    « aucun pari recommandé » à chacun de ses passages).
  - Moteur muet depuis plus de SCAN_STALE_H ou Supabase KO : alerte.
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
    "euroleague_basketball": "🏀",
    "rugby": "🏉", "rugbyleague": "🏉", "aussierules": "🦘",
    "volleyball": "🏐", "tabletennis": "🏓", "handball": "🤾",
}
ELITE_EDGE     = _ELITE_EDGE
SCAN_STALE_H   = 2      # Alerte si aucun scan depuis X heures

# « Nouveau » = né depuis le digest précédent. Doit rester ALIGNÉ sur le cron
# du job `rapport` de reports.yml (toutes les 2 h). Si le cron change,
# changer ici — sinon un signal est 🆕 deux fois, ou jamais.
REPORT_WINDOW_H = 2

# Telegram limite un message à 4096 chars.
_BUDGET = 4000


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


def _parse_dt(raw) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _match_dt(s: dict) -> datetime | None:
    return _parse_dt(s.get("match_time") or "")


def _a_venir(signals: list, now: datetime) -> list:
    """Ne garde que les signaux dont le coup d'envoi est encore devant.

    Le digest de 15:20 le 2026-08-28 annonçait « CSKA Moscow @ 1.39 » sur un
    match commencé à 15:00 : `status='active'` ne tombe qu'à l'audit suivant
    (toutes les 6 h), et le filtre sur `created_at` ne regarde pas le match.
    Un signal sans coup d'envoi lisible est conservé — on ne sait pas.
    """
    out = []
    for s in signals:
        mt = _match_dt(s)
        if mt is None or mt > now:
            out.append(s)
    return out


def _partitionner(signals: list, now: datetime) -> tuple[list, list]:
    """(nouveaux, rappels) — nouveau = créé depuis le digest précédent.

    `created_at` porte la PREMIÈRE émission (run_engine._save met à jour en
    place) : un re-scan du même pari ne le rajeunit pas en 🆕. Chaque liste
    est triée par coup d'envoi (le plus proche d'abord), les sans-horaire à
    la fin. Un signal sans `created_at` lisible compte comme nouveau —
    mieux vaut un doublon qu'un pari jamais annoncé.
    """
    cutoff = now - timedelta(hours=REPORT_WINDOW_H)
    nouveaux, rappels = [], []
    for s in signals:
        c = _parse_dt(s.get("created_at"))
        (nouveaux if c is None or c >= cutoff else rappels).append(s)

    def _ko(s):
        mt = _match_dt(s)
        return (mt is None, mt or now)

    return sorted(nouveaux, key=_ko), sorted(rappels, key=_ko)


def _kickoff(s: dict, now: datetime) -> str:
    """' · 21:00 UTC' today, ' · 22/07 21:00 UTC' otherwise, '' if unknown."""
    mt = _match_dt(s)
    if mt is None:
        return ""
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


def _composer(nouveaux: list, rappels: list, now: datetime,
              stale: bool = False, learning: list | None = None) -> str | None:
    """Le message du digest, ou None s'il n'y a rien à dire.

    Deux sections, 🆕 puis ⏳, chaque ligne au format opérateur. On tronque
    entre deux paris complets, jamais au milieu d'une entité Markdown (un
    `*gras*` coupé affiche des astérisques littéraux). Moteur muet + rien à
    lister : le message ne contient que l'alerte.
    """
    if not nouveaux and not rappels and not stale:
        return None

    header = f"🎯 *PREDATOR* · {now.strftime('%d/%m %H:%M')} UTC\n"
    if stale:
        header += f"⚠️ _Moteur muet depuis plus de {SCAN_STALE_H} h — vérifiez GitHub Actions → Predator Scan_\n"
    if not nouveaux and not rappels:
        return header

    counts = []
    if nouveaux:
        counts.append(f"🆕 `{len(nouveaux)}` nouveau(x)")
    if rappels:
        counts.append(f"⏳ `{len(rappels)}` encore jouable(s)")
    header += " · ".join(counts) + "\n"

    footer = "\n🔥 Élite | ✅ Value | 📌 Faible"
    if learning:
        footer += "\n\n🧠 *Learning* (dernier cycle)\n" + "".join(f"   • {l}\n" for l in learning[:5])

    budget = _BUDGET - len(header) - len(footer) - 80
    body = ""
    truncated = False
    for titre, groupe in (("🆕 *Nouveaux*", nouveaux), ("⏳ *Encore jouables*", rappels)):
        if not groupe:
            continue
        bloc = f"\n{titre}\n"
        if len(body) + len(bloc) > budget:
            truncated = True
            break
        body += bloc
        for s in groupe:
            line = _signal_line(s, now)
            if len(body) + len(line) > budget:
                truncated = True
                break
            body += line
        if truncated:
            break
    if truncated:
        body += "…_(liste tronquée — voir le dashboard)_\n"

    return header + body + footer


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

    # ── Heartbeat : le moteur a-t-il tourné récemment ? ───────────────
    stale = True
    try:
        res = sb.table("meta").select("value").eq("key", "last_scan").limit(1).execute()
        if res.data:
            import json
            last_dt = _parse_dt(json.loads(res.data[0]["value"]).get("at"))
            if last_dt is not None:
                age_h = (now - last_dt).total_seconds() / 3600
                stale = age_h > SCAN_STALE_H
                if stale:
                    log.warning("Dernier scan il y a %.1fh — moteur potentiellement en erreur", age_h)
    except Exception as e:
        log.warning("Meta heartbeat: %s", e)

    # ── Signaux RECOMMANDÉS encore jouables ───────────────────────────
    # is_shadow = false : le digest est une RECOMMANDATION. Jusqu'au
    # 2026-09-03 il relistait les fantômes (T-2h) que le moteur venait de
    # taire — la même tranche mesurée perdante repartait sur Telegram par la
    # porte du rapport. sql/migrate_v10_12.
    # Pas de fenêtre sur created_at : un pari émis à 05:00 pour 18:45 doit
    # rester listé jusqu'au coup d'envoi (en ⏳), sinon l'opérateur ne le
    # voit qu'une fois et demande « où sont mes signaux ? ».
    try:
        res = (sb.table("signals")
               .select("*")
               .eq("status", "active")
               .eq("is_shadow", False)
               .order("match_time", desc=False)
               .limit(60)
               .execute())
        signals = _a_venir(res.data or [], now)
    except Exception as e:
        log.error("Fetch signaux: %s", e)
        _send(
            f"⚠️ *PREDATOR ALERTE ERREUR*\n"
            f"`{now.strftime('%d/%m/%Y %H:%M UTC')}`\n"
            f"Erreur lecture signaux: `{e}`"
        )
        return

    nouveaux, rappels = _partitionner(signals, now)
    log.info("%d paris jouables : %d nouveaux (< %d h), %d rappels",
             len(signals), len(nouveaux), REPORT_WINDOW_H, len(rappels))

    # core/learning_layer.py::compute_and_save persiste un résumé en clair de
    # ce que le dernier audit a changé (seuils) : ici, l'opérateur le voit.
    try:
        learning = _load_learning_summary(sb) or []
    except Exception as e:
        log.warning("Learning summary: %s", e)
        learning = []

    msg = _composer(nouveaux, rappels, now, stale=stale, learning=learning)
    if msg is None:
        log.info("Rien à annoncer, moteur vivant — silence")
        return
    _send(msg)
    log.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")


if __name__ == "__main__":
    run()
