"""
core/daily_quota.py — compteur de requêtes par jour, partagé entre les runs.

POURQUOI
--------
Les sources gratuites se comptent en requêtes PAR JOUR (api-sports : 100 par
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
