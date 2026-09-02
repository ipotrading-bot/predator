"""
core/ai_search.py — façade de complétion IA, entièrement déléguée au routeur.

HISTORIQUE, POUR NE PAS REPAYER LES ENQUÊTES :
  - 2026-07-21 : le grounding Google Search de Gemini gratuit meurt
    (« limit: 0 » vérifié sur 4 clés/projets) → remplacement par Groq
    compound-mini (recherche web intégrée) + Tavily (étage 2).
  - 2026-08-22 (mission 4) : la chaîne de repli passe au ROUTEUR
    (`core/ai_router.py`) — registre, lanes, disjoncteurs, découverte de
    catalogues. Ce module gardait le client Groq direct et Tavily.
  - 2026-09-02 : GROQ ET TAVILY SONT SUPPRIMÉS (décision opérateur — « j'en
    ai marre de groq et tavily toujours épuisé »). Leurs deux quotas gratuits
    lâchaient ENSEMBLE (famines de settlement des 26/08 et 01/09) pour des
    besoins qui n'exigeaient pas d'IA : les scores sont désormais lus dans
    des API structurées (`core/score_sources.py`), les prix sharp viennent
    exclusivement de sources réelles, et la recherche web n'a PLUS AUCUN
    consommateur — `ai_search_complete`, `tavily_search`, la rotation de clés
    Groq, les budgets `groq_search`/`tavily` et `search_exhausted`/
    `search_credits_left`/`prioriser_settlement` sont partis avec.

CE QUI RESTE : `ai_complete` (complétion SANS recherche web, cache 30 min,
routée sur les fournisseurs du registre) pour les consommateurs légitimes
d'IA — le dictionnaire d'alias CJK notamment. `ai_available()` dit s'il
existe au moins un fournisseur de production configuré.
"""
import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone

from core import daily_quota

log = logging.getLogger("PREDATOR.ai_search")

# Cache de réponses (même requête normalisée, fenêtre 30 min) dans meta : le
# même slate ne doit jamais être recherché deux fois dans la même fenêtre —
# souvent plus rentable que de la capacité en plus. Sans Supabase : pas de
# cache, on appelle (même dégradation que daily_quota).
AI_CACHE_TTL_MIN = int(os.environ.get("AI_CACHE_TTL_MIN", "30"))


def _cache_key(prompt: str, queries: list[str] | None) -> str:
    norm = re.sub(r"\s+", " ", (prompt or "").strip().lower())
    qn = "|".join(sorted(re.sub(r"\s+", " ", q.strip().lower()) for q in (queries or [])))
    return "ai_cache_" + hashlib.md5(f"{norm}||{qn}".encode()).hexdigest()[:20]


def _cache_get(key: str) -> str | None:
    sb = daily_quota._db()
    if sb is None:
        return None
    try:
        row = sb.table("meta").select("value,updated_at").eq("key", key).maybe_single().execute()
        if not row or not row.data:
            return None
        ts = datetime.fromisoformat(str(row.data.get("updated_at")).replace("Z", "+00:00"))
        if (datetime.now(timezone.utc) - ts).total_seconds() > AI_CACHE_TTL_MIN * 60:
            return None
        return json.loads(row.data["value"])
    except Exception as e:
        log.debug("ai cache get: %s", e)
        return None


def _cache_put(key: str, text: str) -> None:
    sb = daily_quota._db()
    if sb is None or not text:
        return
    try:
        sb.table("meta").upsert({"key": key, "value": json.dumps(text),
                                 "updated_at": datetime.now(timezone.utc).isoformat()},
                                on_conflict="key").execute()
    except Exception as e:
        log.debug("ai cache put: %s", e)


def providers_available() -> list[str]:
    """Fournisseurs configurés (clé présente).

    Délègue au registre du routeur : un fournisseur ajouté là-bas est
    disponible ici sans toucher à ce fichier — c'était tout l'objet de la
    mission 4.
    """
    from core import ai_router
    return [p.name for p in ai_router.active_providers()]


def ai_available() -> bool:
    """Au moins un fournisseur IA configuré ? (clé présente dans l'env).

    Historiquement « une clé Groq existe » ; depuis le 2026-09-02 c'est le
    registre entier qui répond. Sans aucun fournisseur, les consommateurs
    dégradent en silence (alias non résolus…) — même contrat qu'avant.
    """
    return bool(providers_available())


def _fallback_post(messages: list, max_tokens: int, temperature: float,
                   timeout: int, label: str, lane: str = "analyze") -> str | None:
    """Complétion routée — `core/ai_router.py` applique clés, disjoncteurs,
    budgets et bascule de modèle. Rend None si aucun fournisseur sain ne
    répond — le caller dégrade comme avant."""
    from core import ai_router
    try:
        text, _provider = ai_router.route(messages, lane, label,
                                          max_tokens, temperature, timeout)
        return text
    except Exception as e:                      # jamais d'exception au caller
        log.warning("%s: routeur indisponible (%s)", label, e)
        return None


# Lane par défaut déduite du palier, pour que les call sites existants
# gardent leur comportement sans rien changer. Un appelant qui SAIT ce qu'il
# fait déclare sa lane explicitement — c'est le cas du dictionnaire d'alias
# (lane CJK).
_TIER_LANE = {"light": "filter", "heavy": "analyze"}


def ai_complete(prompt: str, label: str = "AI",
                max_tokens: int = 2048, temperature: float = 0.1,
                timeout: int = 45, tier: str = "heavy",
                lane: str | None = None) -> str | None:
    """Complétion SANS recherche web (connaissance interne du modèle).
    `tier` choisit la lane par défaut du routeur ("light" → filter,
    "heavy" → analyze) ; `lane` la force. Cache 30 min sur la requête
    normalisée. Rend None sans fournisseur sain."""
    ck = _cache_key(prompt, None)
    hit = _cache_get(ck)
    if hit:
        log.info("%s: cache IA (fenêtre %d min) — aucun appel", label, AI_CACHE_TTL_MIN)
        return hit
    messages = [{"role": "user", "content": prompt}]
    text = _fallback_post(messages, max_tokens, temperature, timeout, label,
                          lane or _TIER_LANE.get(tier, "analyze"))
    if text:
        _cache_put(ck, text)
    return text
