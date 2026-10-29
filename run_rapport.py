"""
run_rapport.py — PREDATOR PAIM — job `rapport` de .github/workflows/reports.yml
(toutes les 2 h à H+35) : les ALERTES TECHNIQUES, et rien d'autre.

Décisions opérateur du 2026-10-29 : Telegram porte les paris à jouer (scan,
avec les paris encore jouables), les résultats et le bilan de la veille
(audit), le rapport du lundi — et les alertes de problème technique. Ce job
ne liste plus de paris (le digest 🆕/⏳ doublait le scan) ; il ne parle que
s'il y a un problème :
  - Supabase injoignable ;
  - moteur muet depuis plus de SCAN_STALE_H (aucun scan, quel que soit le
    mode) ;
  - créneau `standard` DÛ et non servi au-delà de CRENEAU_GRACE_MIN — le
    signe que le chien de garde Cloudflare s'est tu (2026-09-11). Règle dure
    n°12 : on surveille le CRÉNEAU DÛ, jamais la fraîcheur d'un fichier.
Tant que le problème dure, l'alerte revient à chaque passage (2 h).
"""
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

from core.db import get_db
from scripts.ci_scan_mode import (SLOT_CLAIM_KEY, SLOT_CLAIM_TTL_MIN, SLOT_META_KEY,
                                  due_slot)

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

SCAN_STALE_H = 2      # alerte si aucun scan depuis X heures

# Créneau `standard` dû et non servi : alerte au-delà de cette grâce. Le chien
# de garde Cloudflare sert un créneau à H+7 au plus tard (grâce 5 min + tick
# de 10 min), puis le scan réclame le créneau à sa première minute. 20 min
# laissent ce chemin s'accomplir et restent SOUS le retard du cron GitHub
# seul (+28 à +104 min mesurés le 2026-09-11). tests/test_rapport_alertes.py
# tient la borne basse alignée sur le Worker (grace_min + 10).
CRENEAU_GRACE_MIN = 20


def _send(text: str) -> None:
    """Texte brut : un message d'alerte ne doit jamais être refusé pour une
    mise en forme. Ne lève jamais."""
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT):
        log.info("Telegram non configuré — alerte non envoyée :\n%s", text)
        return
    try:
        r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                          json={"chat_id": TELEGRAM_CHAT, "text": text}, timeout=10)
        if r.status_code != 200:
            log.error("Telegram HTTP %d : %s", r.status_code, r.text[:200])
    except Exception as e:                                       # noqa: BLE001
        log.error("Telegram : %s", e)


def _parse_dt(raw) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _creneau_manque(slot_servi: str | None, claim: str | None, now: datetime,
                    grace_min: int = CRENEAU_GRACE_MIN) -> str | None:
    """« HH:MM (+N min) » si le dernier créneau `standard` DÛ n'est ni servi ni
    en cours au-delà de la grâce, sinon None. Pur : les deux marques de
    scripts/ci_scan_mode.py (`scan_standard_slot`, `scan_standard_slot_claim`)
    sont passées en clair.

    L'alerte « moteur muet » ne voit pas un standard perdu : les ticks reprice
    horaires gardent `last_scan` frais. Le 2026-09-11, le chien de garde
    lui-même s'est tu (Cloudflare « Workers Cron Triggers degraded ») : 11:03
    jamais servi, tous les runs livrés verts, et rien ne le disait."""
    slot = due_slot(now)
    if slot_servi == slot:
        return None
    if claim and "|" in claim:
        s, ts = claim.split("|", 1)
        try:
            age = now - datetime.fromisoformat(ts)
        except ValueError:
            age = None
        if s == slot and age is not None and timedelta(0) <= age < timedelta(minutes=SLOT_CLAIM_TTL_MIN):
            return None                    # scan en cours : il réclame avant de payer
    retard_min = (now - datetime.fromisoformat(slot).replace(tzinfo=timezone.utc)).total_seconds() / 60
    if retard_min < grace_min:
        return None
    return f"{slot[11:]} (+{retard_min:.0f} min)"


def _creneau_standard_manque(sb, now: datetime) -> str | None:
    """_creneau_manque() nourri par `meta` (clé anon : SELECT autorisé).
    Jamais bloquant : base illisible = pas d'alerte, comme le heartbeat."""
    try:
        res = (sb.table("meta").select("key,value")
               .in_("key", [SLOT_META_KEY, SLOT_CLAIM_KEY]).execute())
        marques = {r["key"]: r.get("value") for r in (res.data or [])}
    except Exception as e:                                       # noqa: BLE001
        log.warning("Meta créneau standard: %s", e)
        return None
    manque = _creneau_manque(marques.get(SLOT_META_KEY), marques.get(SLOT_CLAIM_KEY), now)
    if manque:
        log.warning("Créneau standard %s non servi — chien de garde muet ? "
                    "(python scripts/ops.py watchdog)", manque)
    return manque


def _moteur_muet(sb, now: datetime) -> float | None:
    """Heures depuis le dernier scan s'il dépasse SCAN_STALE_H, sinon None.
    Base illisible : pas d'alerte (l'erreur est journalisée)."""
    try:
        res = sb.table("meta").select("value").eq("key", "last_scan").limit(1).execute()
        if not res.data:
            return None
        dernier = _parse_dt(json.loads(res.data[0]["value"]).get("at"))
    except Exception as e:                                       # noqa: BLE001
        log.warning("Meta heartbeat: %s", e)
        return None
    if dernier is None:
        return None
    age_h = (now - dernier).total_seconds() / 3600
    return age_h if age_h > SCAN_STALE_H else None


def composer_alertes(now: datetime, muet_h: float | None = None,
                     creneau: str | None = None) -> str | None:
    """Le message d'alerte, ou None s'il n'y a aucun problème. Pur."""
    lignes = []
    if muet_h is not None:
        lignes.append(f"⚠️ Moteur muet depuis {muet_h:.1f} h — vérifier GitHub Actions → Predator Scan")
    if creneau:
        lignes.append(f"⚠️ Créneau de scan {creneau} non servi — chien de garde muet ? "
                      f"(python scripts/ops.py watchdog)")
    if not lignes:
        return None
    return f"⚠️ PREDATOR · {now:%d/%m %H:%M} UTC\n" + "\n".join(lignes)


def run():
    now = datetime.now(timezone.utc)
    log.info("PREDATOR RAPPORT — %s — alertes techniques seulement",
             now.strftime("%Y-%m-%d %H:%M UTC"))
    try:
        sb = get_db(write=False)
        if sb is None:
            raise RuntimeError("SUPABASE_URL/SUPABASE_KEY non configurés")
    except Exception as e:                                       # noqa: BLE001
        log.error("Supabase KO: %s", e)
        _send(f"⚠️ PREDATOR · {now:%d/%m %H:%M} UTC\n⚠️ Base Supabase injoignable : {e}")
        return
    msg = composer_alertes(now, _moteur_muet(sb, now), _creneau_standard_manque(sb, now))
    if msg is None:
        log.info("Aucun problème technique — silence")
        return
    _send(msg)


if __name__ == "__main__":
    run()
