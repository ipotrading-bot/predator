"""
core/meta_retention.py — `meta` ne grossit plus sans fin (2026-10-29).

Audit général du 2026-10-29 : `meta` n'était jamais purgée. Quatre familles
de clés DATÉES s'y ajoutent chaque jour — compteurs de quota (`quota_*`, ~9
par jour), demande OddsAPI (`oddsapi_demande_*`), causes de non-capture de
clôture (`closing_causes_*`) et mesure des books candidats (`mesure_books_*`,
20 à 80 Ko PAR JOUR). La table est lue à chaque scan et chaque audit.

Ce qui relit ces historiques, et jusqu'où (vérifié dans le code ce jour-là) :
le rapport hebdo lit 7 jours de `closing_causes_*`, `ops.py candidats N` lit
N jours de `mesure_books_*`, `quota_*` ne sert que le jour même. Garder
RETENTION_JOURS = 60 laisse un large historique d'analyse manuelle.

Ce ne sont PAS des lignes de résultats (règle 9) : le ledger et les signaux ne
sont jamais touchés ici. Seules les familles nommées dans PREFIXES_DATES
partent, et seulement si leur dernière écriture a plus de RETENTION_JOURS.
"""
import logging
from datetime import datetime, timedelta, timezone

log = logging.getLogger("PREDATOR.meta_retention")

RETENTION_JOURS = 60

# Familles de clés `meta` à suffixe de DATE, une clé par jour. Toute autre
# clé (seuils, plafonds, verdicts, curseurs, caches) n'est jamais purgée.
PREFIXES_DATES = ("quota_", "oddsapi_demande_", "closing_causes_", "mesure_books_")


def purger(sb, now: datetime | None = None) -> int:
    """Retire les clés datées plus vieilles que RETENTION_JOURS. Renvoie le
    nombre de clés retirées. Ne lève jamais : une purge manquée se rattrape
    au passage suivant, elle ne doit pas faire tomber l'audit."""
    if sb is None:
        return 0
    seuil = ((now or datetime.now(timezone.utc)) - timedelta(days=RETENTION_JOURS)).isoformat()
    total = 0
    for prefixe in PREFIXES_DATES:
        try:
            res = (sb.table("meta").delete()
                   .like("key", f"{prefixe}%")
                   .lt("updated_at", seuil)
                   .execute())
            total += len(res.data or [])
        except Exception as e:                                   # noqa: BLE001
            log.warning("rétention meta [%s] : %s — reportée au prochain passage",
                        prefixe, str(e)[:80])
    if total:
        log.info("rétention meta : %d clé(s) datée(s) de plus de %d jours retirée(s)",
                 total, RETENTION_JOURS)
    return total
