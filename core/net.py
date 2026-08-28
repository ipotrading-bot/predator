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
import urllib.error
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

    # ── UN PROXY POSÉ L'EMPORTE SUR LE RELAIS (2026-08-27) ───────────
    # La règle inverse (« le relais gagne si les deux sont posés ») datait du
    # 2026-08-26, quand le relais était le seul mécanisme et qu'on ignorait
    # encore d'où il sortirait. On le sait maintenant, et c'est tranché : un
    # Worker s'exécute au colo le plus proche de l'APPELANT, donc IAD depuis
    # les runners GitHub, et 500.com REFUSE cette IP de sortie (run engine
    # 32994959190). Le relais est donc PROUVÉ inopérant là où le pipeline
    # tourne, tandis que le proxy est le remède documenté.
    #
    # Garder l'ancienne précédence menait au pire scénario possible :
    # l'opérateur pose un proxy pour débloquer la source, le relais continue
    # de capter l'URL, et rien ne change — sans un seul message d'erreur qui
    # le dise. Une capacité payée et jamais utilisée, invisible dans les logs.
    #
    # Poser un proxy est un geste EXPLICITE : il n'a qu'une raison d'être, et
    # c'est de contourner exactement ce blocage.
    if proxy_for(source):
        log.info("net[%s]: proxy configuré — le relais est ignoré pour cette "
                 "source (le relais sort au colo de l'appelant, ce que 500.com "
                 "refuse depuis les runners)", source)
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


# ── REPRISE SUR ÉCHEC PASSAGER (2026-08-28) ───────────────────────────
# Un proxy gratuit et partagé est instable par construction. Mesuré le
# 2026-08-28 sur le proxy Webshare qui a débloqué odds500 : sur trois GET
# identiques, **un timeout de handshake TLS à 40 s et deux réponses en ~1 s**.
#
# Sans reprise, cette unique requête ratée coûte la SOURCE ENTIÈRE pour le
# run : `_get` rend None, le calendrier est vide, et odds500 logge « 0 match
# dans les 24h » — indiscernable d'un blocage réel. On vient de payer un
# proxy pour lever un blocage ; le perdre un run sur trois sur un aléa
# réseau serait absurde.
#
# UNE seule reprise, et seulement sur les échecs de TRANSPORT (timeout,
# connexion refusée, coupure TLS). Un 403 ou un 404 est une réponse du
# serveur : la rejouer ne changerait rien et ne ferait que marteler la
# source — c'est ce que `robots.txt` et le budget journalier existent pour
# éviter.
_TRANSIENT = (TimeoutError, ConnectionError, urllib.error.URLError, OSError)


def open_with_retry(source: str, req, timeout: int, tentatives: int = 2):
    """Ouvre `req` en reprenant UNE fois sur un échec de transport.

    Rend l'objet réponse ouvert (à utiliser dans un `with`). Lève la dernière
    exception si toutes les tentatives échouent — l'appelant garde son
    `except` et son message, on ne change que le nombre d'essais.

    La reprise ne s'applique QU'aux échecs de transport : un `HTTPError`
    (403, 404, 429…) est une réponse et remonte immédiatement.
    """
    opener = opener_for(source)
    derniere = None
    for essai in range(1, max(1, tentatives) + 1):
        try:
            if opener is not None:
                return opener.open(req, timeout=timeout)
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError:
            raise                      # une réponse, pas un aléa réseau
        except _TRANSIENT as e:
            derniere = e
            if essai < tentatives:
                log.info("net[%s]: échec de transport (%s) — nouvelle tentative "
                         "%d/%d", source, e, essai + 1, tentatives)
    raise derniere


def describe_failure(source: str, exc: Exception) -> str:
    """Message de log qui distingue « injoignable » de « en panne ».

    Les deux se ressemblent dans un log de cron et n'appellent PAS la même
    action : un refus de connexion depuis un runner veut dire « fournis un
    proxy », une erreur de parsing veut dire « le site a changé ». Confondre
    les deux, c'est ce qui a laissé odds500 muette trois jours sans que la
    cause soit nommée.
    """
    # 403 EN MODE RELAIS : deux causes qui n'appellent pas la même action, et
    # que le seul code HTTP ne distingue pas. Le Worker pose `X-Relay-By` sur
    # toute réponse qu'il a RELAYÉE ; ses propres refus (jeton, hôte) ne le
    # portent pas. Et `cf-ray` nomme le colo Cloudflare qui a exécuté le
    # Worker — donc l'IP de sortie vue par l'amont. Mesuré le 2026-08-26 :
    # 200 via le relais depuis un poste de dev (colo LHR), 403 depuis les
    # runners GitHub (colos US) avec le MÊME jeton — la piste géographique
    # ne se voit qu'avec ce colo dans le log.
    if isinstance(exc, urllib.error.HTTPError) and exc.code == 403 and relay_for(source):
        hdrs = exc.headers or {}
        ray = str(hdrs.get("cf-ray") or "")
        colo = ray.rsplit("-", 1)[-1] if "-" in ray else "?"
        if hdrs.get("X-Relay-By"):
            return (f"{source}: 403 de l'AMONT via le relais (colo Cloudflare {colo}) "
                    f"— le site refuse l'IP de sortie de cet edge ; ni le jeton ni "
                    f"le code ne sont en cause")
        return (f"{source}: 403 du RELAIS lui-même (colo {colo}) — jeton "
                f"{source.upper()}_RELAY_TOKEN/{_RELAY_TOKEN_ENV} désaccordé avec le "
                f"Worker, ou hôte hors liste blanche")
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
