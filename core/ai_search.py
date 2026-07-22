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
pour le fallback. Sans GROQ_API_KEY, tout retourne None/[] silencieusement
(même dégradation que l'ancien `if not api_key: return []`).
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

# Quota JOURNALIER mort — PAR MODÈLE, pas global (corrigé 2026-07-22).
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
# Note : groq/compound-mini n'a pas de quota propre — il consomme celui du
# modèle qui l'exécute (llama-3.3-70b-versatile). Quand le corps d'erreur
# nomme un autre modèle que celui demandé, les DEUX sont marqués morts.
_groq_dead_models: set = set()

_ALL_MODELS = ["groq/compound-mini", "llama-3.3-70b-versatile", "llama-3.1-8b-instant"]


def ai_available() -> bool:
    return bool(os.environ.get("GROQ_API_KEY"))


def ai_dead() -> bool:
    """True quand PLUS AUCUN modèle Groq n'est utilisable ce process.

    Les appelants qui écrivent un état terminal (core/audit_engine.py) s'en
    servent pour décider « je n'ai pas pu chercher » : tant qu'un seul modèle
    répond encore, il reste une chance de settler pour de vrai.
    """
    return all(m in _groq_dead_models for m in _ALL_MODELS)


def _mark_dead(model: str, body: str) -> None:
    """Marque `model` mort + tout modèle connu nommé dans le corps d'erreur."""
    _groq_dead_models.add(model)
    for known in _ALL_MODELS:
        if known in body:
            _groq_dead_models.add(known)


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
    """Un POST Groq avec retry sur erreurs réseau/429-minute/5xx.
    Retourne le texte de la réponse ou None."""
    if model in _groq_dead_models:
        return None
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        return None

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
                _mark_dead(model, body)
                log.critical("%s[%s]: quota JOURNALIER Groq épuisé — modèles morts=%s | %s",
                             label, model, sorted(_groq_dead_models), body)
                return None
            wait = 20 if attempt == 0 else 40
            log.warning("%s[%s]: rate limit minute — attente %ds", label, model, wait)
            time.sleep(wait)
            continue

        if r.status_code == 413:
            # "Request Entity Too Large" — le modèle agentique compound-mini
            # injecte ses résultats de recherche dans le contexte et dépasse
            # le budget tokens/minute (8000 TPM en free tier). Ce n'est PAS
            # une erreur dure : on abandonne ce modèle immédiatement pour
            # retomber sur l'étage Tavily (snippets compacts, bien moins de
            # tokens) via le retour None ci-dessous.
            log.info("%s[%s]: 413 (contexte > budget TPM) — bascule sur le fallback", label, model)
            return None

        if r.status_code >= 500:
            log.warning("%s[%s]: HTTP %d (tentative %d/3)", label, model, r.status_code, attempt + 1)
            time.sleep(5 * (attempt + 1))
            continue

        if r.status_code != 200:
            log.error("%s[%s]: HTTP %d: %s", label, model, r.status_code, r.text[:300])
            return None

        try:
            return r.json()["choices"][0]["message"]["content"] or ""
        except Exception as e:
            log.error("%s[%s]: parse réponse: %s", label, model, e)
            return None

    return None


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
