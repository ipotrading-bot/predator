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
    ESPN_PROXY=...                                  # override par source

Le nom de la source est celui passé à `opener_for()`. L'override par source
gagne : deux hôtes ne sont pas forcément bloqués ensemble.

CONSOMMATEUR depuis le 2026-09-03 : core/score_sources.py (ESPN, TheSportsDB,
MLB statsapi — les scores du settlement). odds500 et 7M, pour qui ce module
est né, sont RETIRÉES ce jour-là (mur anti-bot EdgeOne, décision opérateur) ;
les mesures citées plus bas datent de leur époque et restent vraies du
chemin runner → proxy.

RELAIS CLOUDFLARE RETIRÉ LE 2026-09-10 — UN SEUL MÉCANISME, LE PROXY
---------------------------------------------------------------------
Le relais (Worker qui refaisait la requête depuis SON adresse) avait été posé
le 2026-08-26 pour odds500 et prouvé inopérant depuis les runners (colo IAD
refusé). Il est resté configuré — secret global FREE_SOURCES_RELAY transmis
au pool `scan` — après la suppression du proxy le 2026-09-09 : `relay_for`
a alors capté ESPN, et le Worker, à liste blanche VIDE, a répondu 403 à
chaque scoreboard pendant 14 h. Lu comme « ESPN muet », ce 403 a fait écarter
75 à 90 % des matchs de chaque scan standard (32 → 4 réglables le 10 à
11:10) et aucun signal n'a été émis le 2026-09-10. Depuis un poste de dev,
ESPN répondait 200 avec l'UA du code : la source n'a jamais refusé.
Un secret oublié ne doit plus pouvoir détourner une source : le relais est
retiré du code, des pools de `scripts/ci_env.py` et du dépôt (Worker,
Smart Placement). `prepare()` reste comme couture, identité. Gardien :
`tests/test_free_sources_wiring.py::TestRelaisRetire`.
"""
import logging
import os
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


def prepare(source: str, url: str, headers: dict) -> tuple:
    """(url, en-têtes) à utiliser réellement pour joindre `url` — IDENTITÉ.

    Couture conservée pour les appelants (core/score_sources). Le relais qui
    réécrivait l'URL vers un Worker Cloudflare est RETIRÉ le 2026-09-10 (en
    tête de fichier) : rien ne réécrit plus une URL ici, et une variable
    `FREE_SOURCES_RELAY` qui traînerait dans un environnement est ignorée.
    """
    return url, headers


def opener_for(source: str):
    """`urllib` opener routé par proxy, ou None si aucun n'est configuré.

    None n'est pas une erreur : c'est le cas nominal. L'appelant retombe alors
    sur `urllib.request.urlopen`, exactement comme avant ce module.
    """
    url = proxy_for(source)
    if not url or _memo.get(f"proxy-mort:{source.lower()}"):
        return None
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": url, "https": url}))


# ── PROXY MORT ≠ SOURCE MORTE (2026-09-09) ────────────────────────────
# Mesuré ce jour : le tunnel Webshare répondait « 402 Payment Required »
# (quota du plan épuisé) à CHAQUE ouverture. `open_with_retry` rejouait trois
# fois le même tunnel, ESPN passait pour « muet », le filtre de réglabilité
# écartait 36 à 45 matchs par scan et deux scans standard payants sont sortis
# à 0 signal — alors que l'audit, qui sort en direct, réglait normalement.
# Un refus du TUNNEL (402/407/5xx du proxy, « Tunnel connection failed ») ne
# dit rien de la source : on le note une fois par processus et par source,
# et on sort EN DIRECT pour le reste du run. Si la source refuse l'IP du
# runner, ce sera un 403 de la source, nommé comme tel par describe_failure.
_PROXY_MORT = ("Tunnel connection failed", "Payment Required",
               "Proxy Authentication Required", "Bad Gateway", "Service Unavailable")


def _proxy_mort(exc: Exception) -> bool:
    txt = str(exc)
    return any(m in txt for m in _PROXY_MORT)


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
# DEUX reprises au plus (3 tentatives), et seulement sur les échecs de
# TRANSPORT (timeout, connexion refusée, coupure TLS). Deux ne suffisaient
# pas : mesuré depuis un runner le 2026-08-28, les DEUX tentatives ont échoué
# sur le même scan (« handshake timed out » puis « Remote end closed
# connection »), alors que le même proxy rendait 6/6 depuis un poste de dev.
# Le chemin runner → proxy est plus fragile que le chemin dev → proxy, et les
# échecs se GROUPENT. À ~1 échec sur 3, trois tentatives ramènent le risque
# de perdre la source de 11 % à 4 %, pour une requête de plus en cas d'échec
# seulement.
# Un 403 ou un 404 est une réponse du serveur : la rejouer ne changerait rien
# et ne ferait que marteler la source — c'est ce que `robots.txt` et le
# budget journalier existent pour éviter.
_TRANSIENT = (TimeoutError, ConnectionError, urllib.error.URLError, OSError)


_TENTATIVES = int(os.environ.get("FREE_SOURCES_TENTATIVES", "3"))


def open_with_retry(source: str, req, timeout: int, tentatives: int | None = None):
    """Ouvre `req` en reprenant UNE fois sur un échec de transport.

    Rend l'objet réponse ouvert (à utiliser dans un `with`). Lève la dernière
    exception si toutes les tentatives échouent — l'appelant garde son
    `except` et son message, on ne change que le nombre d'essais.

    La reprise ne s'applique QU'aux échecs de transport : un `HTTPError`
    (403, 404, 429…) est une réponse et remonte immédiatement.
    """
    tentatives = _TENTATIVES if tentatives is None else tentatives
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
            if opener is not None and _proxy_mort(e):
                log.warning("net[%s]: proxy HS (%s) — sortie DIRECTE pour le reste "
                            "du run", source, e)
                _memo[f"proxy-mort:{source.lower()}"] = "1"
                opener = None
                continue               # même tentative, sans le tunnel
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
