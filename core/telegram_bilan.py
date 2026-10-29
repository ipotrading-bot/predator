"""
core/telegram_bilan.py — les deux messages de l'audit (2026-10-29).

Décisions opérateur du 2026-10-29 — Telegram « simple, lisible » :
  1. les paris à jouer — run_engine._telegram_signals, au scan, suivis
     TOUJOURS des paris encore jouables (`section_encore_jouables`) ;
  2. les RÉSULTATS après chaque audit — `annoncer_resultats`, suivis eux
     aussi des paris encore jouables ;
  3. le BILAN de la veille, une fois par jour — `annoncer_bilan_veille`,
     au format demandé : « 28/10 · 4 gagnés · 1 perdu · taux 80 % » ;
  4. les ALERTES de problème technique (run_engine._journaliser_alerte,
     audit stérile, run_rapport) et le rapport du LUNDI (weekly_report).
Ne partent plus : le digest 🆕/⏳ toutes les 2 h, le résumé d'apprentissage
quotidien, la ligne « N matchs analysés · écartés ».

Le « taux » nu du bilan est une EXCEPTION voulue à la règle 7 : l'opérateur
l'a demandé tel quel pour ce message. Les analyses gardent Wilson.

Seuls les paris RECOMMANDÉS comptent (jamais un fantôme) : ce sont ceux qui
ont été annoncés comme « à jouer ». La vérité est le LEDGER (un pari réel =
une ligne, jumeaux écartés), daté par le coup d'envoi du signal. Texte
brut, sans Markdown : un nom d'équipe à « _ » ou « * » ne fait jamais
refuser le message.
"""
import logging
import os
from datetime import datetime, timedelta, timezone

log = logging.getLogger("PREDATOR.telegram_bilan")

BILAN_KEY = "bilan_veille"     # meta : jour (AAAA-MM-JJ) du dernier bilan ENVOYÉ
BILAN_HEURE_UTC = 6            # le bilan part au premier audit à partir de 06:00 UTC
_ICONES = {"WIN": "✅", "LOSS": "❌", "PUSH": "↩️"}


def envoyer(texte: str) -> bool:
    """True si Telegram a pris le message. Ne lève jamais."""
    jeton, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (jeton and chat):
        log.info("Telegram non configuré — message non envoyé :\n%s", texte)
        return False
    try:
        import requests
        r = requests.post(f"https://api.telegram.org/bot{jeton}/sendMessage", timeout=10,
                          json={"chat_id": chat, "text": texte})
        if r.status_code != 200:
            log.warning("Telegram HTTP %d : %s", r.status_code, r.text[:200])
            return False
        return True
    except Exception as e:                                       # noqa: BLE001
        log.warning("Telegram : %s", e)
        return False


# ── Paris encore jouables (décision opérateur du 2026-10-29) ─────────────
# « Mentionne toujours les paris encore jouables » : en fin du message des
# paris (scan) et du message des résultats (audit). Recommandés seulement,
# statut actif, coup d'envoi devant nous.

_MAX_JOUABLES = 25      # au-delà, « … et N autres » : un message reste lisible


def _heure(match_time, now: datetime) -> str:
    try:
        t = datetime.fromisoformat(str(match_time).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return "?"
    t = t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    return f"{t:%H:%M}" if t.date() == now.date() else f"{t:%d/%m %H:%M}"


def _sans_markdown(texte: str) -> str:
    """Le message des paris part en Markdown : un « _ » ou un « * » dans un
    nom d'équipe le ferait refuser par Telegram."""
    for c in ("\\", "_", "*", "`", "["):
        texte = texte.replace(c, "\\" + c)
    return texte


def lire_encore_jouables(sb, now: datetime, exclure: set | None = None) -> list[dict]:
    """Recommandés actifs au coup d'envoi futur, du plus proche au plus loin.
    `exclure` : clés (match, market_key, selection_name) déjà détaillées dans
    le même message. Base illisible → [] (jamais bloquant)."""
    if sb is None:
        return []
    try:
        res = (sb.table("signals")
               .select("match,market_key,selection_name,xbet_odd,match_time,soft_book")
               .eq("status", "active")
               .eq("is_shadow", False)
               .gt("match_time", now.isoformat())
               .order("match_time")
               .limit(100)
               .execute())
    except Exception as e:                                       # noqa: BLE001
        log.warning("paris encore jouables illisibles : %s", e)
        return []
    exclure = exclure or set()
    return [s for s in (res.data or [])
            if (s.get("match"), s.get("market_key"), s.get("selection_name")) not in exclure]


def section_encore_jouables(paris: list[dict], now: datetime, markdown: bool = False) -> str:
    """« ⏳ Encore jouables (N) » puis une ligne par pari ; "" s'il n'y en a pas."""
    if not paris:
        return ""
    esc = _sans_markdown if markdown else (lambda t: t)
    lignes = []
    for s in paris[:_MAX_JOUABLES]:
        try:
            cote = f" @ {float(s.get('xbet_odd')):.2f}"
        except (TypeError, ValueError):
            cote = ""
        book = f" · {s['soft_book']}" if s.get("soft_book") else ""
        lignes.append(esc(f"• {s.get('match', '?')} · {_heure(s.get('match_time'), now)} → "
                          f"{s.get('selection_name') or '?'}{cote}{book}"))
    if len(paris) > _MAX_JOUABLES:
        lignes.append(f"… et {len(paris) - _MAX_JOUABLES} autres")
    return f"⏳ Encore jouables ({len(paris)})\n" + "\n".join(lignes)


# ── 2. Résultats après chaque audit ──────────────────────────────────────

def message_resultats(lignes: list[dict]) -> str | None:
    """« 📋 Résultats » puis une ligne par pari réglé. None si rien."""
    regles = [l for l in lignes if l.get("outcome") in _ICONES]
    if not regles:
        return None
    corps = []
    for l in regles:
        cote = l.get("odds")
        try:
            cote_txt = f" @ {float(cote):.2f}" if cote is not None else ""
        except (TypeError, ValueError):
            cote_txt = ""
        rembourse = " (remboursé)" if l["outcome"] == "PUSH" else ""
        corps.append(f"{_ICONES[l['outcome']]} {l.get('match', '?')} — "
                     f"{l.get('selection') or '?'}{cote_txt}{rembourse}")
    return "📋 Résultats\n" + "\n".join(corps)


def annoncer_resultats(sb, depuis: datetime) -> int:
    """Envoie les paris RECOMMANDÉS entrés au ledger depuis `depuis` (début
    de l'audit). Rend le nombre de paris annoncés. Ne lève jamais."""
    try:
        res = (sb.table("ai_learning_ledger")
               .select("match,selection,odds,outcome,created_at,is_shadow")
               .gte("created_at", depuis.isoformat())
               .eq("is_shadow", False)
               .order("created_at")
               .execute())
        lignes = res.data or []
    except Exception as e:                                       # noqa: BLE001
        log.warning("résultats de l'audit illisibles : %s", e)
        return 0
    texte = message_resultats(lignes)
    if texte is None:
        return 0
    jouables = section_encore_jouables(lire_encore_jouables(sb, datetime.now(timezone.utc)),
                                       datetime.now(timezone.utc))
    envoyer(texte + ("\n\n" + jouables if jouables else ""))
    return sum(1 for l in lignes if l.get("outcome") in _ICONES)


# ── 3. Bilan de la veille ────────────────────────────────────────────────

def message_bilan(jour: datetime, gagnes: int, perdus: int,
                  rembourses: int = 0, en_attente: int = 0) -> str | None:
    """« 📊 28/10 · 4 gagnés · 1 perdu · taux 80 % ». None si rien à dire."""
    if not (gagnes or perdus or rembourses or en_attente):
        return None
    morceaux = [f"📊 {jour:%d/%m}",
                f"{gagnes} gagné{'s' if gagnes > 1 else ''}",
                f"{perdus} perdu{'s' if perdus > 1 else ''}"]
    if gagnes + perdus:
        morceaux.append(f"taux {round(100 * gagnes / (gagnes + perdus))} %")
    if rembourses:
        morceaux.append(f"{rembourses} remboursé{'s' if rembourses > 1 else ''}")
    if en_attente:
        morceaux.append(f"{en_attente} en attente")
    return " · ".join(morceaux)


def _bilan_du_jour(sb, jour: datetime) -> tuple[int, int, int, int]:
    """(gagnés, perdus, remboursés, en attente) des RECOMMANDÉS dont le match
    s'est joué ce jour-là (UTC)."""
    debut = jour.replace(hour=0, minute=0, second=0, microsecond=0)
    fin = debut + timedelta(days=1)
    res = (sb.table("signals")
           .select("id,status")
           .eq("is_shadow", False)
           .gte("match_time", debut.isoformat())
           .lt("match_time", fin.isoformat())
           .execute())
    signaux = res.data or []
    en_attente = sum(1 for s in signaux if s.get("status") == "active")
    ids = [s["id"] for s in signaux if s.get("status") != "active"]
    issues = []
    if ids:
        res = (sb.table("ai_learning_ledger").select("signal_id,outcome")
               .in_("signal_id", ids).execute())
        issues = [r.get("outcome") for r in (res.data or [])]
    return (issues.count("WIN"), issues.count("LOSS"), issues.count("PUSH"), en_attente)


def annoncer_bilan_veille(sb, now: datetime | None = None) -> bool:
    """Bilan d'HIER, une seule fois par jour, au premier audit à partir de
    BILAN_HEURE_UTC. Le jour envoyé est noté dans meta.bilan_veille ; un envoi
    raté n'est pas noté et repart à l'audit suivant. Ne lève jamais."""
    now = now or datetime.now(timezone.utc)
    if now.hour < BILAN_HEURE_UTC:
        return False
    hier = now - timedelta(days=1)
    cle_jour = hier.strftime("%Y-%m-%d")
    try:
        res = sb.table("meta").select("value").eq("key", BILAN_KEY).limit(1).execute()
        if res.data and res.data[0].get("value") == cle_jour:
            return False
        texte = message_bilan(hier, *_bilan_du_jour(sb, hier))
    except Exception as e:                                       # noqa: BLE001
        log.warning("bilan de la veille : %s", e)
        return False
    if texte is not None and not envoyer(texte):
        return False
    try:
        sb.table("meta").upsert({"key": BILAN_KEY, "value": cle_jour,
                                 "updated_at": now.isoformat()}).execute()
    except Exception as e:                                       # noqa: BLE001
        log.warning("bilan de la veille : jour non noté (%s) — risque de doublon", e)
    return texte is not None
