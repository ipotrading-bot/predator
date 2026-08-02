"""
core/ai_search.py — Groq + Tavily = remplacement de Gemini Search grounding.

Pourquoi ce module existe (2026-07-21) : le grounding Google Search de
l'API Gemini gratuite est mort — "limit: 0" sur generate_content_free_tier
pour TOUTE clé/projet sans compte de facturation Gemini prépayé (vérifié
sur 4 clés/projets indépendants, dont un avec le crédit d'essai Cloud
$300 actif : le crédit d'essai GCP n'est PAS éligible au prépaiement
Gemini API). Décision opérateur : supprimer Gemini, pas de nouveau coût.

Remplacement en 2 étages, même contrat que les anciens call sites Gemini
(prompt → texte brut contenant du JSON, le caller parse) :

  1. groq/compound-mini — modèle agentique Groq avec RECHERCHE WEB
     INTÉGRÉE (vérifié live 2026-07-21 : retourne de vrais résultats
     UFC datés). Gratuit, ne consomme aucun crédit Tavily.
  2. Fallback : recherche Tavily (1 000 crédits/mois gratuits) +
     extraction llama-3.3-70b-versatile.

Les appels SANS recherche (estimateur Tier 3) vont directement sur
llama-3.3-70b-versatile → llama-3.1-8b-instant.

Env requis : GROQ_API_KEY (gsk_...) ; optionnel : TAVILY_API_KEY (tvly-...)
pour le fallback, et GROQ_API_KEY_2 / GROQ_API_KEY_3 pour la rotation quand
le quota journalier d'un compte est épuisé (voir _groq_keys). Sans aucune
clé Groq, tout retourne None/[] silencieusement (même dégradation que
l'ancien `if not api_key: return []`).
"""
import json
import logging
import os
import time

import requests

log = logging.getLogger("PREDATOR.ai_search")

GROQ_URL   = "https://api.groq.com/openai/v1/chat/completions"
TAVILY_URL = "https://api.tavily.com/search"

# Vérifiés live sur ce compte le 2026-07-21 (GET /models) :
_SEARCH_MODEL   = "groq/compound-mini"     # recherche web intégrée
_EXTRACT_MODELS = ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"]

# Budget Tavily par process — protège le quota mensuel (1 000 crédits)
# contre un run pathologique. Chaque search basic = 1 crédit.
_TAVILY_RUN_BUDGET = int(os.environ.get("TAVILY_RUN_BUDGET", "25"))
_tavily_used = 0

# Quota JOURNALIER mort — PAR MODÈLE **ET PAR CLÉ** (par-modèle 2026-07-22,
# par-clé 2026-08-02).
#
# Groq applique le TPD (tokens par jour) séparément à chaque modèle :
# llama-3.3-70b-versatile a 100 000 TPD, llama-3.1-8b-instant a un plafond
# bien plus large. Un flag global faisait qu'une seule 429 "per day" sur le
# 70b court-circuitait AUSSI le 8b, qui avait encore tout son quota — le
# settlement de core/audit_engine.py rendait alors None toute la journée,
# le ledger ne recevait plus rien et /performance restait figé (constaté sur
# les runs audit 29886717393 / 29904315408 / 29926411707 du 2026-07-22 :
# « 0 settled | 0 closed | 0 expired »).
#
# Le TPD est compté PAR ORGANISATION, pas par clé : une 2e clé créée sur le
# MÊME compte Groq ne rachète aucun quota (même piège que le connecteur
# web_search Mistral). GROQ_API_KEY_2 n'a d'intérêt que si elle vient d'un
# autre compte — vérifiable dans le corps de n'importe quelle 429/413, qui
# nomme l'org (`in organization org_...`). Vérifié le 2026-08-02 :
# clé 1 = org_01kqe4e69de36sapwgbr3sg3d7, clé 2 = org_01kz1yaaskehrtw18nr3knbvty.
#
# Note : groq/compound-mini n'a pas de quota propre — il consomme celui du
# modèle qui l'exécute (llama-3.3-70b-versatile). Quand le corps d'erreur
# nomme un autre modèle que celui demandé, les DEUX sont marqués morts.
_groq_dead_models: dict[int, set] = {}

_ALL_MODELS = ["groq/compound-mini", "llama-3.3-70b-versatile", "llama-3.1-8b-instant"]

# Le TPD Groq (100k tokens/jour) est compté PAR ORGANISATION : la seule façon
# d'élargir la réserve en gratuit est d'ajouter une clé d'un AUTRE compte.
# Depuis le cloisonnement du 2026-08-02, ces variables ne sont plus servies
# uniformément : audit.yml ne reçoit que la clé du settlement (sous le nom
# GROQ_API_KEY), les workflows de scan reçoivent les autres. C'est le workflow
# qui décide de la réserve, pas ce module — d'où l'ordre simple ci-dessous.
_KEY_ENVS = ("GROQ_API_KEY", "GROQ_API_KEY_2", "GROQ_API_KEY_3", "GROQ_API_KEY_4")


def _groq_keys() -> list[str]:
    """Clés Groq configurées, dans l'ordre d'essai, dédupliquées.

    La même valeur collée dans deux secrets ne compte qu'une fois : elle
    partagerait de toute façon le quota, et la déduplication évite d'annoncer
    dans les logs une bascule qui ne rachète rien.
    """
    keys: list[str] = []
    for env in _KEY_ENVS:
        v = (os.environ.get(env) or "").strip()
        if v and v not in keys:
            keys.append(v)
    return keys


def _dead(key_idx: int) -> set:
    return _groq_dead_models.setdefault(key_idx, set())


def ai_available() -> bool:
    return bool(_groq_keys())


def ai_dead() -> bool:
    """True quand PLUS AUCUN modèle Groq n'est utilisable ce process.

    Les appelants qui écrivent un état terminal (core/audit_engine.py) s'en
    servent pour décider « je n'ai pas pu chercher » : tant qu'un seul modèle
    répond encore, sur n'importe laquelle des clés, il reste une chance de
    settler pour de vrai.
    """
    keys = _groq_keys()
    if not keys:
        # Aucune clé configurée : rien n'a jamais été marqué mort. On garde le
        # False historique — ai_available() est le test que font les appelants
        # pour ce cas-là, et rendre True ici ferait écrire des états terminaux
        # à core/audit_engine.py sur une simple absence de secret.
        return False
    return all(
        all(m in _dead(i) for m in _ALL_MODELS)
        for i in range(len(keys))
    )


def _mark_dead(key_idx: int, model: str, body: str) -> None:
    """Marque `model` mort sur CETTE clé + tout modèle connu nommé dans l'erreur."""
    dead = _dead(key_idx)
    dead.add(model)
    for known in _ALL_MODELS:
        if known in body:
            dead.add(known)


def search_exhausted() -> bool:
    """True quand l'étage 2 (Tavily) ne peut plus rien servir pour ce process.

    Distinct de ai_dead() : le quota JOURNALIER Groq peut être intact alors que
    compound-mini se fait jeter en rate-limit MINUTE à répétition — dans ce cas
    Tavily est le seul filet, et une fois son budget de run épuisé,
    ai_search_complete() renvoie None pour tout le reste du run.

    Les appelants qui écrivent un état TERMINAL (core/audit_engine.py) doivent
    tester ceci avant de conclure : un None renvoyé dans cet état veut dire
    « je n'ai pas pu chercher », pas « l'information n'existe pas ».
    """
    return _tavily_used >= _TAVILY_RUN_BUDGET


def search_credits_left() -> int:
    """Crédits Tavily encore disponibles pour ce process.

    Permet à un appelant de RÉSERVER ce qui reste au travail le plus
    important : dans core/audit_engine.py, le settlement (résultat réel
    WIN/LOSS, permanent) prime toujours sur la CLV (une métrique).
    """
    return max(0, _TAVILY_RUN_BUDGET - _tavily_used)


def _groq_post(model: str, messages: list, max_tokens: int,
               temperature: float, timeout: int, label: str):
    """Un POST Groq, en basculant de clé quand la courante ne peut plus servir.

    Retourne le texte de la réponse ou None. Les clés sont essayées dans
    l'ordre de `_KEY_ENVS` ; on ne passe à la suivante que si la courante est
    épuisée (quota jour, ou rate-limit minute qui résiste aux 3 tentatives),
    jamais sur une erreur qui se reproduirait à l'identique ailleurs (413,
    payload invalide) — sinon une requête trop grosse brûlerait les deux clés.
    """
    keys = _groq_keys()
    if not keys:
        return None

    for idx, api_key in enumerate(keys):
        if model in _dead(idx):
            continue
        text, rotate = _groq_post_one(idx, api_key, model, messages,
                                      max_tokens, temperature, timeout, label)
        if text is not None:
            return text
        if not rotate:
            return None
        if idx + 1 < len(keys):
            log.warning("%s[%s]: clé #%d inutilisable — bascule sur la clé #%d",
                        label, model, idx + 1, idx + 2)
    return None


def _groq_post_one(key_idx: int, api_key: str, model: str, messages: list,
                   max_tokens: int, temperature: float, timeout: int,
                   label: str) -> tuple[str | None, bool]:
    """Un POST Groq sur UNE clé, avec retry réseau/429-minute/5xx.

    Retourne (texte, rotate). `rotate` dit si une AUTRE clé aurait une chance
    de réussir là où celle-ci a échoué.
    """
    payload = {
        "model":       model,
        "messages":    messages,
        "max_tokens":  max_tokens,
        "temperature": temperature,
    }
    headers = {"Authorization": f"Bearer {api_key}",
               "Content-Type": "application/json"}

    for attempt in range(3):
        try:
            r = requests.post(GROQ_URL, json=payload, headers=headers, timeout=timeout)
        except Exception as e:
            log.warning("%s[%s]: erreur réseau (tentative %d/3): %s", label, model, attempt + 1, e)
            time.sleep(5 * (attempt + 1))
            continue

        if r.status_code == 429:
            body = r.text[:500]
            # Groq distingue per-minute (retry utile) et per-day (terminal)
            # dans le message d'erreur ("per day"/"TPD"/"RPD").
            if "per day" in body.lower() or "tpd" in body.lower() or "rpd" in body.lower():
                _mark_dead(key_idx, model, body)
                log.critical("%s[%s]: quota JOURNALIER Groq épuisé sur la clé #%d — "
                             "modèles morts=%s | %s",
                             label, model, key_idx + 1, sorted(_dead(key_idx)), body)
                return None, True
            wait = 20 if attempt == 0 else 40
            log.warning("%s[%s]: rate limit minute (clé #%d) — attente %ds",
                        label, model, key_idx + 1, wait)
            time.sleep(wait)
            continue

        if r.status_code == 413:
            # "Request Entity Too Large" — le modèle agentique compound-mini
            # injecte ses résultats de recherche dans le contexte et dépasse
            # le budget tokens/minute (8000 TPM en free tier). Ce n'est PAS
            # une erreur dure : on abandonne ce modèle immédiatement pour
            # retomber sur l'étage Tavily (snippets compacts, bien moins de
            # tokens) via le retour None ci-dessous.
            # Le TPM est identique sur l'autre clé : la même requête y échouerait
            # pareil, donc on ne rotationne pas — l'étage Tavily est le vrai plan B.
            log.info("%s[%s]: 413 (contexte > budget TPM) — bascule sur le fallback", label, model)
            return None, False

        if r.status_code >= 500:
            log.warning("%s[%s]: HTTP %d (tentative %d/3)", label, model, r.status_code, attempt + 1)
            time.sleep(5 * (attempt + 1))
            continue

        if r.status_code != 200:
            log.error("%s[%s]: HTTP %d (clé #%d): %s",
                      label, model, r.status_code, key_idx + 1, r.text[:300])
            # Clé révoquée/invalide : la suivante, elle, peut être bonne. C'est
            # exactement le scénario de rotation de clé morte du 2026-07-12.
            return None, r.status_code in (401, 403)

        try:
            return r.json()["choices"][0]["message"]["content"] or "", False
        except Exception as e:
            log.error("%s[%s]: parse réponse: %s", label, model, e)
            return None, False

    # 3 tentatives épuisées (réseau, 5xx, ou rate-limit MINUTE tenace) : l'autre
    # clé a ses propres compteurs par minute, ça vaut le coup de la tenter.
    return None, True


def tavily_search(query: str, max_results: int = 5) -> list[dict]:
    """Recherche Tavily basic (1 crédit). Retourne [{title,url,content}] ou []."""
    global _tavily_used
    api_key = os.environ.get("TAVILY_API_KEY")
    if not api_key:
        return []
    if _tavily_used >= _TAVILY_RUN_BUDGET:
        log.warning("Tavily: budget du run épuisé (%d) — recherche sautée", _TAVILY_RUN_BUDGET)
        return []
    try:
        r = requests.post(TAVILY_URL, json={
            "api_key":     api_key,
            "query":       query,
            "max_results": max_results,
            "search_depth": "basic",
        }, timeout=20)
        _tavily_used += 1
        if r.status_code != 200:
            log.warning("Tavily HTTP %d: %s", r.status_code, r.text[:200])
            return []
        return [
            {"title": x.get("title", ""), "url": x.get("url", ""),
             "content": x.get("content", "")}
            for x in r.json().get("results", [])
        ]
    except Exception as e:
        log.warning("Tavily: %s", e)
        return []


def ai_complete(prompt: str, label: str = "AI",
                max_tokens: int = 2048, temperature: float = 0.1,
                timeout: int = 45) -> str | None:
    """Complétion SANS recherche web (connaissance interne du modèle).
    Équivalent de l'ancien appel Gemini sans tools."""
    messages = [{"role": "user", "content": prompt}]
    for model in _EXTRACT_MODELS:
        text = _groq_post(model, messages, max_tokens, temperature, timeout, label)
        if text:
            return text
    return None


def ai_search_complete(prompt: str, queries: list[str], label: str = "AI",
                       max_tokens: int = 2048, temperature: float = 0.1,
                       timeout: int = 60) -> str | None:
    """
    Complétion AVEC recherche web — remplaçant direct de
    Gemini + "tools":[{"google_search":{}}].

    Étage 1 : groq/compound-mini (recherche intégrée, gratuit).
    Étage 2 : Tavily (queries) + extraction llama-3.3-70b.
    Retourne le texte brut (le caller extrait/parse le JSON) ou None.
    """
    # ── Étage 1 : compound-mini fait sa propre recherche ──────────────
    messages = [{"role": "user", "content": prompt}]
    text = _groq_post(_SEARCH_MODEL, messages, max_tokens, temperature, timeout, label)
    if text and text.strip():
        return text

    # compound-mini mort ne veut PAS dire abandon : son quota est celui du
    # 70b, alors que l'étage 2 peut encore tourner sur llama-3.1-8b-instant.
    # On ne renonce que si plus aucun modèle ne répond.
    if ai_dead():
        return None

    # ── Étage 2 : Tavily + extraction ─────────────────────────────────
    snippets: list[str] = []
    for q in queries[:4]:
        for res in tavily_search(q):
            snippets.append(f"[{res['title']}] {res['content']}")
    if not snippets:
        log.warning("%s: compound-mini KO et aucune donnée Tavily — abandon", label)
        return None

    context = "\n\n".join(snippets[:20])
    grounded = (
        "Web search results (use ONLY this data, do not invent facts):\n"
        f"{context}\n\n---\n\n{prompt}"
    )
    messages = [{"role": "user", "content": grounded}]
    for model in _EXTRACT_MODELS:
        text = _groq_post(model, messages, max_tokens, temperature, timeout, label)
        if text:
            return text
    return None
