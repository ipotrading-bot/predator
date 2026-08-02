"""
core/secret_store.py — lecture des secrets rotatifs depuis Supabase (v10.1)

POURQUOI CE MODULE EXISTE
-------------------------
Le plan OddsAPI fait 500 requêtes/mois et s'épuise en ~30 h. Rotation de clé
tous les 1-2 jours, donc — et chaque rotation coûtait DEUX manips manuelles à
l'opérateur, dans deux interfaces différentes :

  1. secret GitHub Actions `ODDS_API_KEY`  → alimente le moteur (run_engine)
  2. variable d'env Vercel `ODDS_API_KEY`  → alimente le widget quota du
     dashboard, et n'a effet qu'après un *Redeploy* explicite

Les deux se désynchronisaient systématiquement : le dashboard a affiché
`0 restantes / 500 utilisées` alors que la nouvelle clé était intacte, parce
que Vercel portait encore l'ancienne. Pire, rien dans l'UI ne dit laquelle des
deux clés répond — voir la note « Vercel porte une AUTRE clé » de core/db.py.

Ici, la table `app_secrets` (sql/migrate_v10_1_app_secrets.sql) devient la
source de vérité unique. Une rotation = un UPDATE SQL, pris en compte par le
moteur ET par le dashboard au prochain appel, sans redéploiement.

ORDRE DE RÉSOLUTION : Supabase d'abord, os.environ ensuite
----------------------------------------------------------
Volontairement dans cet ordre. Si l'env gagnait, une variable Vercel oubliée
continuerait d'écraser la clé fraîche — c'est exactement le bug qu'on
supprime. L'env reste un filet : table absente (migration pas encore
appliquée), pas de service_role, panne réseau Supabase → on retombe sur le
comportement d'avant, jamais sur une panne.

Corollaire à connaître : une valeur PÉRIMÉE dans `app_secrets` gagne contre un
env correct. La table est la source de vérité, il faut la traiter comme telle.

ACCÈS : service_role uniquement
-------------------------------
`app_secrets` a RLS activé sans aucune policy — seul service_role, qui
contourne RLS, peut lire. get_db(write=True) est donc utilisé ici pour un
usage en LECTURE : le flag ne veut pas dire « je vais écrire », il veut dire
« donne-moi le client privilégié ». Un déploiement sans SUPABASE_SERVICE_KEY
(cas possible sur Vercel) ne lève pas : il tombe simplement sur l'env.
"""
import logging
import os
import time

from core.db import get_db, MissingCredentialsError

log = logging.getLogger("PREDATOR.secret_store")

TABLE = "app_secrets"

# Cache process-local. Sur Vercel une lambda vit quelques secondes, sur
# Actions un run vit quelques minutes : ce TTL évite surtout de refrapper
# Supabase à chaque ligue scannée dans une même boucle, sans jamais retarder
# une rotation de plus de 5 min.
_TTL_S = 300
_cache: dict[str, tuple[float, str | None]] = {}
_logged_source: dict[str, str] = {}


def _log_source_once(name: str, source: str) -> None:
    """Un seul log par (secret, source) et par process — cette fonction est
    appelée dans des boucles de scan, on ne veut pas 19 lignes identiques."""
    if _logged_source.get(name) != source:
        _logged_source[name] = source
        log.info("Secret %s résolu depuis %s", name, source)


def _from_supabase(name: str) -> str | None:
    try:
        sb = get_db(write=True)
    except MissingCredentialsError as e:
        log.debug("secret_store: pas de service_role (%s) — fallback env", e)
        return None
    if sb is None:
        return None
    try:
        res = (sb.table(TABLE).select("value")
               .eq("key", name).limit(1).execute())
    except Exception as e:
        # Table absente = migration v10_1 pas encore appliquée. C'est un
        # état attendu, pas une panne : on le dit une fois, en debug.
        log.debug("secret_store: lecture %s impossible (%s) — fallback env", name, e)
        return None
    if not res.data:
        return None
    value = (res.data[0].get("value") or "").strip()
    return value or None


def get_secret(name: str, *, env_fallback: bool = True,
               force_refresh: bool = False) -> str | None:
    """Renvoie le secret `name`, ou None s'il est introuvable partout.

    force_refresh contourne le cache — à utiliser quand l'appelant vient de
    constater qu'une clé est morte (401/403) et veut savoir si une rotation
    a eu lieu entre-temps.
    """
    now = time.monotonic()
    if not force_refresh:
        hit = _cache.get(name)
        if hit and (now - hit[0]) < _TTL_S:
            return hit[1]

    value = _from_supabase(name)
    if value:
        _log_source_once(name, "Supabase/app_secrets")
    elif env_fallback:
        value = (os.environ.get(name) or "").strip() or None
        if value:
            _log_source_once(name, "os.environ (fallback)")

    # On ne met en cache QUE les valeurs trouvées. Mémoriser un None ferait
    # traîner 5 min un état « aucune clé nulle part » qui est, par
    # construction, déjà une panne : autant réessayer à chaque appel, c'est
    # au pire une requête sur une base injoignable.
    if value:
        _cache[name] = (now, value)
    return value


def invalidate(name: str | None = None) -> None:
    """Vide le cache — tout, ou un seul secret."""
    if name is None:
        _cache.clear()
    else:
        _cache.pop(name, None)
