"""
run_rapport.py — PREDATOR PAIM — job `rapport` de .github/workflows/reports.yml.

MUET depuis le 2026-10-29 (décision opérateur : « Telegram : les signaux à
jouer, les résultats après chaque audit, les gains/pertes du jour précédent.
Simple, lisible »). Le digest toutes les 2 h — rappel des paris 🆕/⏳,
anomalies d'apprentissage, créneau standard manqué, moteur muet — doublait
les paris déjà annoncés au scan et chargeait Telegram de données que
l'opérateur n'utilisait pas. Les trois messages restants :
  - paris à jouer : run_engine._telegram_signals (scan) ;
  - résultats et bilan de la veille : core/telegram_bilan (audit).

Le job reste planifié : le chien de garde Cloudflare surveille la fraîcheur
de reports.yml (scripts/cloudflare_watchdog_worker.js, stale_min 130) et le
relancerait sans fin s'il disparaissait. Il journalise l'état du moteur —
consultable dans les logs Actions — sans rien envoyer.
"""
import json
import logging
import time
from datetime import datetime, timezone

from dotenv import load_dotenv

from core.db import get_db

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


def run():
    now = datetime.now(timezone.utc)
    log.info("PREDATOR RAPPORT — %s — muet (Telegram : paris, résultats, bilan)",
             now.strftime("%Y-%m-%d %H:%M UTC"))
    try:
        sb = get_db(write=False)
        res = sb.table("meta").select("value").eq("key", "last_scan").limit(1).execute()
        if res.data:
            dernier = json.loads(res.data[0]["value"])
            log.info("Dernier scan : %s · %s match(s) · %s signal(aux)", dernier.get("at"),
                     dernier.get("matches"), dernier.get("signals"))
    except Exception as e:                                       # noqa: BLE001
        log.warning("état du moteur illisible : %s", e)


if __name__ == "__main__":
    run()
