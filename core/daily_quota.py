"""
core/daily_quota.py — compteur de requêtes par jour, partagé entre les runs.

POURQUOI
--------
Les sources gratuites se comptent en requêtes PAR JOUR (odds-api.io : 500 par jour, TheSportsDB, ESPN ; api-sports — retirée — faisait 100 par
sport ; odds-api.io : 500), alors que ce pipeline lance ~40 scans quotidiens
depuis des processus séparés. Aucun d'eux ne sait ce que les autres ont
dépensé : un garde-fou local à un run ne protège rien.

Le compte du projet chez api-sports a été trouvé SUSPENDU le 2026-08-20,
après des mois passés à dépenser une requête par match. Dépasser durablement
un plan gratuit ne se solde pas par un simple 429 : le fournisseur ferme le
compte. D'où ce compteur partagé, stocké dans la table Supabase `meta` sous
`quota_<bucket>_<AAAAMMJJ>`.

DÉGRADATION
-----------
Sans Supabase (tests, sandbox, panne réseau), le compteur rend 0 et n'écrit
rien : la source reste utilisable et on perd seulement le partage entre runs.
Une source de cotes ne doit jamais tomber parce que son compteur est muet.
"""
import logging
from datetime import datetime, timezone

log = logging.getLogger("PREDATOR.daily_quota")


def _key(bucket: str) -> str:
    return f"quota_{bucket}_{datetime.now(timezone.utc):%Y%m%d}"


def _db():
    """Client privilégié, ou None. `write=True` donne le service_role —
    la table `meta` est protégée par RLS (voir core/secret_store.py)."""
    try:
        from core.db import get_db
        return get_db(write=True)
    except Exception as e:                       # credentials absentes incluses
        log.debug("quota: pas de base (%s)", e)
        return None


def spent(bucket: str) -> int:
    """Requêtes déjà dépensées aujourd'hui pour ce compartiment (0 si inconnu)."""
    sb = _db()
    if sb is None:
        return 0
    try:
        row = sb.table("meta").select("value").eq("key", _key(bucket)).maybe_single().execute()
        return int((row.data or {}).get("value") or 0) if row and row.data else 0
    except Exception as e:
        log.debug("quota[%s]: lecture impossible (%s)", bucket, e)
        return 0


def add(bucket: str, n: int) -> None:
    """Ajoute n requêtes au compteur du jour. Silencieux en cas de panne."""
    if n <= 0:
        return
    sb = _db()
    if sb is None:
        return
    try:
        sb.table("meta").upsert(
            {"key": _key(bucket), "value": str(spent(bucket) + n),
             "updated_at": datetime.now(timezone.utc).isoformat()},
            on_conflict="key").execute()
    except Exception as e:
        log.debug("quota[%s]: écriture impossible (%s)", bucket, e)


def remaining(bucket: str, budget: int) -> int:
    return max(0, budget - spent(bucket))


# ── RYTHME DE DÉPENSE (2026-08-27) ────────────────────────────────────
# Un compteur partagé empêche de DÉPASSER un plan. Il ne dit rien de QUAND
# on le dépense, et c'est l'autre moitié du problème : les crons du matin
# raflaient tout, et les scans du soir — quand le slate européen entre dans
# la zone jouable 2-24 h — repartaient sans leur source.
#
# Mesuré le 2026-08-27 sur api-sports : budget épuisé à 19:20, le slate sharp
# du Tier 2 tombant de 42 matchs à 25. Même forme sur Tavily et sur le quota
# Groq, à des heures voisines. La réponse est la même partout, donc elle est
# ICI et pas recopiée dans chaque source : trois copies d'une même règle
# finissent toujours par diverger (règle dure n°6).
def paced_allowance(budget: int, floor: int, now: datetime | None = None) -> int:
    """Part de `budget` ouverte à cette heure de la journée UTC.

    Croît linéairement de `floor` (00:00) à `budget` (24:00). `floor` vaut le
    coût d'un cycle complet : sans lui, le premier run du jour ne pourrait
    rien faire et la source serait morte jusqu'à midi.

    Aucun horaire n'est codé en dur — une fenêtre favorable qui bouge dans
    core/scan_windows.py n'a rien à re-déclarer ici.
    """
    now = now or datetime.now(timezone.utc)
    debut = now.replace(hour=0, minute=0, second=0, microsecond=0)
    frac = (now - debut).total_seconds() / 86400.0
    frac = min(1.0, max(0.0, frac))
    return max(min(floor, budget), min(budget, int(budget * frac)))
