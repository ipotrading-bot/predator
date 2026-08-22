"""
core/wiz_sources.py — sources de recherche de Wiz, en cascade.

POURQUOI (mesuré le 2026-08-01) : sur les 93 analyses de `wiz_analysis`,
**79 sont INDISPONIBLE** (85%). Cause connue et documentée : le connecteur
`web_search` de Mistral a un quota PROPRE, au niveau du compte, épuisé depuis
le 2026-07-23 — `POST /v1/conversations` renvoie 429 `web_search rate limit
reached` pendant que `GET /v1/models` répond 200. Wiz n'avait qu'une source :
quand elle tombe, Wiz est muet, et la page /wiz n'affiche plus qu'un mur de
cartes vides.

LA DISTINCTION QUI DÉBLOQUE TOUT : c'est le CONNECTEUR de recherche qui est
mort, pas le modèle. `/v1/chat/completions` fonctionne, sur la même clé, avec
un quota différent. Le raisonnement de Wiz est donc toujours disponible — il
ne lui manque que des yeux. Ce module lui en redonne :

  1. `mistral_search`  — recherche + raisonnement en un appel (chemin nominal,
                          revient tout seul si le connecteur redevient dispo) ;
  2. Google News RSS   — GRATUIT, sans clé, sans compte, sans carte. Beaucoup
                          de TITRES datés, sur à peu près n'importe quel
                          match ; mais rien de plus que des titres (voir
                          « LE DEUXIÈME PIÈGE » ci-dessous) ;
  3. Bing News RSS     — GRATUIT lui aussi, moins de résultats, mais avec un
                          vrai extrait de l'article et l'URL DIRECTE de
                          l'éditeur. C'est la source qui porte réellement la
                          matière de Wiz : absences, compos, lanceur
                          confirmé, enjeu, météo ;
  4. Tavily            — déjà en place pour le moteur, mais consommé ici sous
                          RÉSERVE (voir plus bas) ;
  puis raisonnement via `mistral_complete`, et en dernier ressort `ai_complete`
  (routeur IA, puis Groq).

LE DEUXIÈME PIÈGE (mesuré le 2026-08-22, /wiz entièrement vide). La cascade
ci-dessus marchait — 5 sources par match, Mistral répondait — et pourtant
100% des analyses sortaient INDISPONIBLE avec « aucune information
exploitable ». Cause : la `<description>` d'un item Google News RSS n'est PAS
un extrait de l'article, c'est le titre suivi du nom du média, en HTML. Après
nettoyage, `content` répétait `title` mot pour mot. Wiz recevait donc une
liste de gros titres et zéro fait — et un modèle à qui l'on interdit
d'inventer répond correctement qu'il n'a rien trouvé. Deux conséquences,
toutes deux tenues par tests/test_wiz_sources.py :

  - un `content` qui n'est que l'écho du titre est VIDÉ (`_echoes_title`),
    pour qu'aucun prompt ne fasse passer un titre pour une source étayée ;
  - les sources gratuites ne se court-circuitent plus l'une l'autre. Avant,
    `gather()` s'arrêtait à la première qui renvoyait quelque chose : Google
    News répondant toujours, Bing n'était jamais interrogé et l'unique source
    porteuse d'extraits restait inaccessible. Les deux sont désormais
    interrogées et FUSIONNÉES ; Tavily ne s'active que si le gratuit n'a
    rien donné du tout.

LA RÉSERVE, INVARIANT À NE PAS CASSER. core/ai_search.py (Groq+Tavily) est le
poumon du MOTEUR : settlement, oracle, harvester. Wiz est une couche
d'agrément. Une journée chargée en analyses contextuelles ne doit jamais
pouvoir empêcher un vrai settlement d'aboutir — c'est la raison d'être de
core/wiz_ai.py comme domaine de panne séparé. Wiz ne touche donc à Tavily que
s'il reste plus de WIZ_TAVILY_RESERVE crédits, et Google News (gratuit,
illimité) passe toujours en premier.

Ni Google News RSS ni Bing News RSS ne sont des API contractuelles : ce sont
des flux publics, sans clé et sans quota, qui peuvent changer de forme sans
préavis. Tout échec de parsing y est donc traité comme « pas de résultat »,
jamais comme une erreur — Wiz retombe alors sur la source suivante, exactement
comme pour Mistral.
"""
import logging
import os
import re
import xml.etree.ElementTree as ET
from urllib.parse import parse_qs, quote_plus, urlparse

import requests

log = logging.getLogger("PREDATOR.wiz_sources")

GOOGLE_NEWS_RSS = "https://news.google.com/rss/search"
BING_NEWS_RSS   = "https://www.bing.com/news/search"

# Bing sert le flux RSS à un navigateur, pas à un client anonyme : sans
# User-Agent crédible il rend une page d'accueil au lieu du XML. Pas d'accent
# ici — un User-Agent accentué fait encoder l'en-tête en latin-1 par urllib et
# déclenche des 403 (le piège déjà rencontré sur Polymarket).
_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# Crédits Tavily que Wiz laisse intacts au moteur. Voir la note « RÉSERVE ».
WIZ_TAVILY_RESERVE = int(os.environ.get("WIZ_TAVILY_RESERVE", "15"))

# Articles conservés par requête. Au-delà, le prompt sature — c'est le mode
# d'échec « Too many content was opened » déjà rencontré sur le connecteur
# Mistral, et il se reproduit à l'identique quand on injecte trop de texte.
MAX_PER_QUERY = 5
MAX_SNIPPET   = 320

# Plafond sur l'ENSEMBLE de la collecte. Depuis que les sources gratuites
# fusionnent au lieu de se court-circuiter, une requête peut en ramener deux
# fois plus : ce plafond garde la même enveloppe de prompt qu'avant.
MAX_TOTAL = 12

# Fenêtre de fraîcheur des articles. Wiz cherche des faits périssables
# (absence, compo, lanceur, météo) : au-delà de quelques jours, une source
# n'est pas moins fiable, elle est fausse. Voir google_news().
FRESHNESS_DAYS = int(os.environ.get("WIZ_FRESHNESS_DAYS", "3"))

_TAG = re.compile(r"<[^>]+>")


_ALNUM = re.compile(r"[^0-9a-z]+")


def _clean(text: str) -> str:
    return _TAG.sub("", text or "").replace("&nbsp;", " ").strip()


def _echoes_title(content: str, title: str, source: str = "") -> bool:
    """Ce « contenu » n'est-il que le titre recopié ?

    C'est le cas de TOUS les items Google News : la description est le titre
    suivi du nom du média. Un tel contenu n'apporte aucun fait, mais il occupe
    une ligne dans le prompt et donne au modèle l'illusion d'une source
    étayée. On le vide plutôt que de le laisser mentir — l'item reste
    présent (titre + URL + date), il annonce juste ce qu'il est : un titre.
    """
    c = _ALNUM.sub("", (content or "").lower())
    t = _ALNUM.sub("", (title or "").lower())
    if not c or not t:
        return False
    reste = c.replace(t, "", 1) if t in c else c
    reste = reste.replace(_ALNUM.sub("", (source or "").lower()), "", 1)
    # Le titre est déjà là et il ne reste presque rien d'autre : pas un extrait.
    return t in c and len(reste) < 25


def google_news(query: str, limit: int = MAX_PER_QUERY,
                within_days: int = FRESHNESS_DAYS) -> list[dict]:
    """Articles Google News pour cette requête — 0 crédit, aucune clé.

    `when:Nd` est OBLIGATOIRE, pas un raffinement. Vérifié live le
    2026-08-01 : sans lui le flux trie par pertinence et remonte des articles
    vieux de plusieurs mois (une finale de Libertadores 2025 pour un
    Flamengo-Palmeiras d'aujourd'hui). Or Wiz cherche exactement le genre de
    fait — « titulaire absent », « lanceur changé » — qui est vrai un jour et
    faux le lendemain : une source périmée n'est pas une source faible, c'est
    une fausse information présentée comme un fait daté.

    Renvoie [{title, url, content, published, source}]. Toujours une liste :
    une panne réseau, un flux vide ou un XML illisible valent « rien trouvé »,
    jamais une exception — Wiz doit pouvoir enchaîner sur la source suivante.
    """
    q = f"{query} when:{max(1, int(within_days))}d"
    url = f"{GOOGLE_NEWS_RSS}?q={quote_plus(q)}&hl=en-US&gl=US&ceid=US:en"
    try:
        r = requests.get(url, timeout=12,
                         headers={"User-Agent": "Mozilla/5.0 (compatible; PredatorWiz/1.0)"})
        if r.status_code != 200:
            log.debug("Google News HTTP %d pour %r", r.status_code, query)
            return []
        root = ET.fromstring(r.content)
    except Exception as e:
        log.debug("Google News %r: %s", query, e)
        return []

    out: list[dict] = []
    for item in root.iter("item"):
        link  = (item.findtext("link") or "").strip()
        title = _clean(item.findtext("title"))
        if not link or not title:
            continue
        src     = _clean(item.findtext("{http://news.google.com/}source")
                         or item.findtext("source"))
        content = _clean(item.findtext("description"))[:MAX_SNIPPET]
        if _echoes_title(content, title, src):
            content = ""     # description = titre recopié : ce n'est pas un extrait
        out.append({
            "title":     title,
            "url":       link,
            "content":   content,
            "published": (item.findtext("pubDate") or "").strip(),
            "source":    src,
        })
        if len(out) >= limit:
            break
    return out


def bing_news(query: str, limit: int = MAX_PER_QUERY) -> list[dict]:
    """Articles Bing News pour cette requête — 0 crédit, aucune clé.

    Deux propriétés que Google News n'a pas, et qui sont la raison d'être de
    cette source : la `<description>` est un VRAI extrait de l'article (une à
    deux phrases de contexte : forme, absence, enjeu), et le `<link>` mène à
    l'éditeur, pas à un redirecteur opaque. Wiz peut donc citer une URL que
    l'opérateur pourra ouvrir — c'est ce que la règle R4 du prompt exige.

    En contrepartie, Bing indexe moins : 0 à 3 résultats par match, souvent
    rien sur les divisions mineures. Les deux sources sont complémentaires,
    pas concurrentes — voir gather().

    Renvoie [{title, url, content, published, source}]. Toujours une liste :
    tout échec vaut « rien trouvé », jamais une exception.
    """
    url = f"{BING_NEWS_RSS}?q={quote_plus(query)}&format=RSS"
    try:
        r = requests.get(url, timeout=12, headers={"User-Agent": _BROWSER_UA})
        if r.status_code != 200:
            log.debug("Bing News HTTP %d pour %r", r.status_code, query)
            return []
        root = ET.fromstring(r.content)
    except Exception as e:
        log.debug("Bing News %r: %s", query, e)
        return []

    out: list[dict] = []
    for item in root.iter("item"):
        link  = _unwrap_bing((item.findtext("link") or "").strip())
        title = _clean(item.findtext("title"))
        if not link or not title:
            continue
        content = _clean(item.findtext("description"))[:MAX_SNIPPET]
        out.append({
            "title":     title,
            "url":       link,
            "content":   "" if _echoes_title(content, title) else content,
            "published": (item.findtext("pubDate") or "").strip(),
            "source":    urlparse(link).netloc.replace("www.", ""),
        })
        if len(out) >= limit:
            break
    return out


def _unwrap_bing(link: str) -> str:
    """L'URL de l'éditeur, derrière le redirecteur de tracking de Bing.

    Bing sert ses liens RSS sous la forme
    `bing.com/news/apiclick.aspx?...&url=<url encodée>`. Laissée telle quelle,
    elle passerait dans le prompt puis dans un argument de Wiz, et l'opérateur
    cliquerait sur un lien de tracking au lieu de l'article.
    """
    if "apiclick.aspx" not in link:
        return link
    real = parse_qs(urlparse(link).query).get("url", [""])[0].strip()
    return real or link


def _tavily(query: str, limit: int = MAX_PER_QUERY) -> list[dict]:
    """Tavily, mais seulement au-dessus de la réserve du moteur."""
    from core import ai_search
    if ai_search.search_credits_left() <= WIZ_TAVILY_RESERVE:
        log.info("Wiz: Tavily laissé au moteur (%d crédit(s) ≤ réserve %d)",
                 ai_search.search_credits_left(), WIZ_TAVILY_RESERVE)
        return []
    return ai_search.tavily_search(query, max_results=limit)


# Les deux « yeux » gratuits, interrogés tous les deux à chaque requête.
# L'ordre fixe la priorité en cas de doublon d'URL, rien de plus.
FREE_SOURCES = (google_news, bing_news)


def _fetch(source, query: str) -> list[dict]:
    """Une source ne casse jamais Wiz : une panne vaut « rien trouvé »."""
    try:
        return source(query) or []
    except Exception as e:
        log.debug("source %s: %s", getattr(source, "__name__", "?"), e)
        return []


def gather(queries: list[str]) -> list[dict]:
    """Sources web pour ces requêtes, dédupliquées par URL, gratuit d'abord.

    Les sources gratuites sont FUSIONNÉES, pas mises en concurrence : Google
    News couvre presque tous les matchs mais ne rend que des titres, Bing rend
    de vrais extraits mais ne couvre que les affiches principales. S'arrêter à
    la première qui répond — ce que faisait la version d'avant le 2026-08-22 —
    revenait à ne jamais interroger Bing, donc à ne jamais obtenir un seul
    extrait, donc à sortir INDISPONIBLE sur 100% des matchs.

    Tavily reste le dernier recours par requête, et seulement au-dessus de la
    réserve du moteur : l'invariant « Wiz ne peut pas affamer un settlement »
    n'est pas touché.
    """
    seen: set = set()
    results: list[dict] = []

    def keep(found: list[dict]) -> None:
        for item in found:
            url = (item.get("url") or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            results.append(item)

    for query in queries:
        # `servie` compte ce que le gratuit a RAMENÉ, pas ce qui a survécu à
        # la déduplication : deux requêtes qui remontent les mêmes articles
        # sont un succès du gratuit, pas une raison d'aller payer Tavily.
        servie = 0
        for source in FREE_SOURCES:
            found = _fetch(source, query)
            servie += len(found)
            keep(found)
        if not servie:       # aucune source gratuite n'a rien pour cette requête
            keep(_fetch(_tavily, query))

    return results[:MAX_TOTAL]


def format_results(results: list[dict]) -> str:
    """Bloc de résultats injecté dans le prompt.

    L'URL est répétée en clair sur chaque entrée parce que la règle R4 du
    prompt exige que chaque argument cite l'URL EXACTE d'une page fournie —
    et que `validate()` rejette tout argument dont l'URL ne figure pas dans
    ce set. Le modèle doit donc pouvoir les recopier sans les deviner.
    """
    lines = []
    for i, r in enumerate(results, 1):
        head = f"[{i}] {r.get('title', '')}".strip()
        meta = " · ".join(x for x in (r.get("source"), r.get("published")) if x)
        lines.append(head + (f"\n    ({meta})" if meta else "")
                     + f"\n    URL: {r.get('url', '')}"
                     + (f"\n    {r.get('content', '')}" if r.get("content") else ""))
    return "\n".join(lines)


def cascade_available() -> bool:
    """Wiz peut-il encore produire une analyse par UNE route quelconque ?

    Les yeux (Google News) sont toujours là — gratuits, sans clé. Ce qui peut
    manquer, c'est le cerveau : il faut au moins un fournisseur de
    raisonnement, Mistral en chat pur OU Groq.

    À utiliser à la place de wiz_ai.wiz_available() partout où l'on décidait
    d'arrêter un run : avant la cascade, un connecteur mort valait fin de
    partie, et c'est ce raccourci qui a produit 85% d'INDISPONIBLE.
    """
    from core import ai_search, wiz_ai
    mistral_ok = wiz_ai.wiz_available() and not wiz_ai.wiz_dead()
    return bool(mistral_ok or ai_search.ai_available())


def make_search_fn(ctx: dict):
    """Le `search_fn` que core/wiz_engine.analyze_match() utilise en prod.

    Même contrat que core.wiz_ai.mistral_search : (prompt, label) →
    (texte, sources, modèle). La cascade est interne, pour que le moteur Wiz
    n'ait à connaître ni les fournisseurs, ni leurs quotas.
    """
    def search_fn(prompt: str, label: str = "WIZ"):
        from core import wiz_ai, wiz_engine

        # 1. Chemin nominal — recherche et raisonnement en un seul appel.
        if not wiz_ai.search_quota_dead():
            text, sources, model = wiz_ai.mistral_search(prompt, label=label)
            if text and sources:
                return text, sources, model
            log.info("Wiz: connecteur de recherche muet — bascule sur les sources externes")

        # 2. Les yeux : Google News (gratuit) puis Tavily (sous réserve).
        queries = wiz_engine.build_queries(
            ctx.get("match", ""), ctx.get("sport", ""),
            ctx.get("market_keys") or [], ctx.get("kickoff") or "")
        sources = gather(queries)
        if not sources:
            return None, [], None

        # 3. Le cerveau : Mistral en chat pur (quota distinct du connecteur),
        #    puis Groq. Les deux reçoivent le MÊME contrat de sortie que le
        #    chemin nominal — c'est validate() qui tranche derrière, à
        #    l'identique, quel que soit le fournisseur.
        grounded = wiz_engine.build_prompt_from_results(ctx, format_results(sources))
        text, model = wiz_ai.mistral_complete(grounded, label=f"{label}/grounded")
        if not text:
            from core import ai_search
            text = ai_search.ai_complete(grounded, label=f"{label}/grounded")
            # PAS "groq" : depuis la mission 4, ai_complete() interroge le
            # ROUTEUR avant Groq, et c'est souvent un autre fournisseur qui
            # répond. `model_used` finit dans wiz_analysis et sert à
            # diagnostiquer une source morte — une étiquette qui ment sur le
            # fournisseur fait chercher la panne au mauvais endroit.
            model = "ai_router" if text else None
        if not text:
            return None, sources, None
        return text, sources, model

    return search_fn
