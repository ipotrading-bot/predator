"""
core/telegram_bilan.py — les deux messages de l'audit (2026-10-29).

Décision opérateur du 2026-10-29 : Telegram ne porte plus que TROIS
messages, « simples, lisibles » :
  1. les paris à jouer — run_engine._telegram_signals, au scan ;
  2. les RÉSULTATS après chaque audit — `annoncer_resultats` ;
  3. le BILAN de la veille, une fois par jour — `annoncer_bilan_veille`,
     au format demandé : « 28/10 · 4 gagnés · 1 perdu · taux 80 % ».
Tout le reste (alertes, résumé d'apprentissage, digest toutes les 2 h,
rapport hebdo) a quitté Telegram ; les journaux et `meta` le gardent.

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
    envoyer(texte)
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
