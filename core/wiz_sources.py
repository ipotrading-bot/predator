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
  2. Google News RSS   — GRATUIT, sans clé, sans compte, sans carte. Renvoie
                          des articles datés avec URL réelle et source. C'est
                          exactement la matière de Wiz : absences, compos,
                          lanceur confirmé, enjeu, météo ;
  3. Tavily            — déjà en place pour le moteur, mais consommé ici sous
                          RÉSERVE (voir plus bas) ;
  puis raisonnement via `mistral_complete`, et en dernier ressort `ai_complete`
  (Groq).

LA RÉSERVE, INVARIANT À NE PAS CASSER. core/ai_search.py (Groq+Tavily) est le
poumon du MOTEUR : settlement, oracle, harvester. Wiz est une couche
d'agrément. Une journée chargée en analyses contextuelles ne doit jamais
pouvoir empêcher un vrai settlement d'aboutir — c'est la raison d'être de
core/wiz_ai.py comme domaine de panne séparé. Wiz ne touche donc à Tavily que
s'il reste plus de WIZ_TAVILY_RESERVE crédits, et Google News (gratuit,
illimité) passe toujours en premier.

Google News RSS n'est pas une API contractuelle : c'est un flux public, sans
clé et sans quota, qui peut changer de forme sans préavis. Tout échec de
parsing y est donc traité comme « pas de résultat », jamais comme une erreur —
Wiz retombe alors sur la source suivante, exactement comme pour Mistral.
"""
import logging
import os
import re
import xml.etree.ElementTree as ET
from urllib.parse import quote_plus

import requests

log = logging.getLogger("PREDATOR.wiz_sources")

GOOGLE_NEWS_RSS = "https://news.google.com/rss/search"

# Crédits Tavily que Wiz laisse intacts au moteur. Voir la note « RÉSERVE ».
WIZ_TAVILY_RESERVE = int(os.environ.get("WIZ_TAVILY_RESERVE", "15"))

# Articles conservés par requête. Au-delà, le prompt sature — c'est le mode
# d'échec « Too many content was opened » déjà rencontré sur le connecteur
# Mistral, et il se reproduit à l'identique quand on injecte trop de texte.
MAX_PER_QUERY = 5
MAX_SNIPPET   = 320

# Fenêtre de fraîcheur des articles. Wiz cherche des faits périssables
# (absence, compo, lanceur, météo) : au-delà de quelques jours, une source
# n'est pas moins fiable, elle est fausse. Voir google_news().
FRESHNESS_DAYS = int(os.environ.get("WIZ_FRESHNESS_DAYS", "3"))

_TAG = re.compile(r"<[^>]+>")


def _clean(text: str) -> str:
    return _TAG.sub("", text or "").replace("&nbsp;", " ").strip()


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
        out.append({
            "title":     title,
            "url":       link,
            "content":   _clean(item.findtext("description"))[:MAX_SNIPPET],
            "published": (item.findtext("pubDate") or "").strip(),
            "source":    _clean(item.findtext("{http://news.google.com/}source")
                                or item.findtext("source")),
        })
        if len(out) >= limit:
            break
    return out


def _tavily(query: str, limit: int = MAX_PER_QUERY) -> list[dict]:
    """Tavily, mais seulement au-dessus de la réserve du moteur."""
    from core import ai_search
    if ai_search.search_credits_left() <= WIZ_TAVILY_RESERVE:
        log.info("Wiz: Tavily laissé au moteur (%d crédit(s) ≤ réserve %d)",
                 ai_search.search_credits_left(), WIZ_TAVILY_RESERVE)
        return []
    return ai_search.tavily_search(query, max_results=limit)


def gather(queries: list[str]) -> list[dict]:
    """Sources web pour ces requêtes, dédupliquées par URL, gratuit d'abord."""
    seen: set = set()
    results: list[dict] = []
    for query in queries:
        for fetch in (google_news, _tavily):
            try:
                found = fetch(query)
            except Exception as e:                  # une source ne casse jamais Wiz
                log.debug("source %s: %s", getattr(fetch, "__name__", "?"), e)
                found = []
            for item in found:
                url = (item.get("url") or "").strip()
                if not url or url in seen:
                    continue
                seen.add(url)
                results.append(item)
            if found:
                break        # requête servie par la source gratuite : on s'arrête là
    return results


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
            model = "groq" if text else None
        if not text:
            return None, sources, None
        return text, sources, model

    return search_fn
