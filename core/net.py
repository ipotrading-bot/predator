"""
core/net.py — sortie réseau des sources filtrées par IP.

POURQUOI CE MODULE EXISTE
--------------------------
Mesuré le 2026-08-26 : `https://odds.500.com/` répond **HTTP 200 et 15
fixtures** depuis un poste de développement, et `Connection refused` depuis
les runners GitHub Actions, à chaque run depuis le 2026-08-23. Le code va
bien, le parseur va bien, le User-Agent va bien — c'est la PLAGE D'IP qui est
refusée. Aucune correction de code ne lève un blocage d'IP.

La seule issue côté logiciel est donc une porte de sortie : router ces
requêtes par un proxy que l'opérateur fournit. Ce module la tient, et rien
d'autre. Sans variable d'environnement, il rend `None` et chaque source garde
EXACTEMENT le comportement qu'elle avait — aucun proxy n'est jamais imposé.

CONFIGURATION
-------------
    FREE_SOURCES_PROXY=http://user:pass@hote:port   # toutes les sources
    ODDS500_PROXY=...                               # override par source
    SEVENM_PROXY=...

Le nom de la source est celui passé à `opener_for()`. L'override par source
gagne : 7M (px-analyse.7mdt.com) et 500.com ne sont pas hébergés au même
endroit et peuvent très bien ne pas être bloqués ensemble — au 2026-08-26,
7M n'a JAMAIS été appelé depuis un runner, donc sa joignabilité réelle
depuis Azure est INCONNUE, pas « bonne ».
"""
import logging
import os
import urllib.parse
import urllib.request

log = logging.getLogger("PREDATOR.net")

_ENV_GLOBAL = "FREE_SOURCES_PROXY"

# Mémo PAR PROCESSUS de la résolution du proxy.
#
# POURQUOI il ne suffit pas de s'appuyer sur le cache de `secret_store` : ce
# dernier ne met en cache que les valeurs TROUVÉES (mémoriser un None ferait
# traîner 5 min un état de panne). Or ici, l'absence de proxy est le cas
# NOMINAL — sans ce mémo, chaque requête HTTP d'odds500 déclencherait une
# lecture Supabase pour se voir répondre « toujours rien ».
#
# Un run dure quelques minutes et personne ne fait tourner un proxy en cours
# de scan : figer la réponse pour la durée du processus est sans risque.
# `reset()` existe pour les tests.
_memo: dict = {}


def proxy_for(source: str) -> str:
    """URL de proxy configurée pour cette source, ou "" s'il n'y en a pas.

    Résolution : `{SOURCE}_PROXY` puis `FREE_SOURCES_PROXY`, chacun lu par
    `secret_store` — donc Supabase/`app_secrets` D'ABORD, environnement
    ensuite. C'est ce qui rend le proxy ROTATIF sans redéploiement, comme les
    clés OddsAPI : une URL qui expire se remplace en base, pas dans un secret
    GitHub qu'il faut ensuite propager à quatre workflows.
    """
    key = source.lower()
    if key in _memo:
        return _memo[key]
    try:
        from core.secret_store import get_secret
        value = (get_secret(f"{source.upper()}_PROXY")
                 or get_secret(_ENV_GLOBAL) or "").strip()
    except Exception as e:                     # best-effort, comme partout ici
        log.debug("net: secret_store indisponible (%s) — lecture directe", e)
        value = (os.environ.get(f"{source.upper()}_PROXY")
                 or os.environ.get(_ENV_GLOBAL) or "").strip()
    _memo[key] = value
    return value


def reset() -> None:
    """Oublie la résolution mémorisée (tests, ou rotation forcée)."""
    _memo.clear()


# ── Mode RELAIS (Cloudflare Worker) ─────────────────────────────────────
#
# Un Worker n'est PAS un proxy HTTP : il ne parle pas CONNECT, donc
# `ProxyHandler` ne sait pas s'en servir. Le relais fonctionne autrement — on
# appelle le Worker en lui passant l'URL cible, et il refait la requête depuis
# SON adresse. D'où deux mécanismes distincts dans ce module, et pas un seul :
#
#     proxy   : la requête part vers odds.500.com via un intermédiaire CONNECT
#     relais  : la requête part vers le Worker, qui va chercher odds.500.com
#
# Le relais gagne sur le proxy si les deux sont configurés (il est plus
# spécifique). Le déploiement du Worker est décrit dans
# `scripts/cloudflare_relay_worker.js`.
#
# ⚠️ CE QUI RESTE INCONNU : rien ne garantit que 500.com accepte les adresses
# de sortie de Cloudflare. Le blocage constaté vise les plages GitHub/Azure ;
# que l'edge Cloudflare passe se VÉRIFIE, ne se suppose pas — `ops.py sources`
# après déploiement, puis un vrai run GitHub Actions.
_RELAY_ENV = "FREE_SOURCES_RELAY"
_RELAY_TOKEN_ENV = "FREE_SOURCES_RELAY_TOKEN"


def _secret(name: str) -> str:
    try:
        from core.secret_store import get_secret
        return (get_secret(name) or "").strip()
    except Exception:
        return (os.environ.get(name) or "").strip()


def relay_for(source: str) -> str:
    """URL du Worker relais pour cette source, ou "" s'il n'y en a pas."""
    key = f"relay:{source.lower()}"
    if key in _memo:
        return _memo[key]
    value = _secret(f"{source.upper()}_RELAY") or _secret(_RELAY_ENV)
    _memo[key] = value
    return value


def prepare(source: str, url: str, headers: dict) -> tuple:
    """(url, en-têtes) à utiliser réellement pour joindre `url`.

    Sans relais configuré, rend l'URL et les en-têtes INCHANGÉS — c'est le
    cas nominal, et il ne coûte rien. Avec relais, l'URL cible passe en
    paramètre `u` et le jeton partagé en en-tête.

    Le jeton n'est pas décoratif : un Worker qui relaie n'importe quelle URL
    pour n'importe qui est un proxy ouvert, que le premier venu utilisera
    pour autre chose. Le Worker vérifie AUSSI une liste blanche d'hôtes.
    """
    relay = relay_for(source)
    if not relay:
        return url, headers
    target = urllib.parse.quote(url, safe="")
    out = dict(headers or {})
    token = _secret(f"{source.upper()}_RELAY_TOKEN") or _secret(_RELAY_TOKEN_ENV)
    if token:
        out["X-Relay-Token"] = token
    return f"{relay.rstrip('/')}?u={target}", out


def opener_for(source: str):
    """`urllib` opener routé par proxy, ou None si aucun n'est configuré.

    None n'est pas une erreur : c'est le cas nominal. L'appelant retombe alors
    sur `urllib.request.urlopen`, exactement comme avant ce module.
    """
    url = proxy_for(source)
    if not url:
        return None
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": url, "https": url}))


def describe_failure(source: str, exc: Exception) -> str:
    """Message de log qui distingue « injoignable » de « en panne ».

    Les deux se ressemblent dans un log de cron et n'appellent PAS la même
    action : un refus de connexion depuis un runner veut dire « fournis un
    proxy », une erreur de parsing veut dire « le site a changé ». Confondre
    les deux, c'est ce qui a laissé odds500 muette trois jours sans que la
    cause soit nommée.
    """
    txt = str(exc)
    refus = ("Connection refused" in txt or "timed out" in txt
             or "Temporary failure in name resolution" in txt
             or "Network is unreachable" in txt)
    if not refus:
        return f"{source}: {exc}"
    if proxy_for(source):
        return (f"{source}: INJOIGNABLE malgré le proxy configuré ({exc}) — "
                f"vérifier le proxy, pas le code")
    return (f"{source}: INJOIGNABLE depuis cet hôte ({exc}) — filtrage par IP "
            f"très probable. Le code n'y peut rien : router par un proxy via "
            f"{source.upper()}_PROXY ou {_ENV_GLOBAL}")
