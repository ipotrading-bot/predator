"""
core/wiz_ai.py — WIZ (PAIM v10.0) : couche fournisseur Mistral.

Pourquoi ce module existe séparément de core/ai_search.py (2026-07-23) :
DOMAINE DE PANNE SÉPARÉ. Le moteur principal (settlement, oracle, harvester)
dépend de Groq + Tavily, dont le quota journalier meurt régulièrement — voir
le long commentaire de core/ai_search.py sur les runs audit du 2026-07-22 où
« 0 settled | 0 closed | 0 expired » trois fois d'affilée. Wiz est une couche
d'agrément : si elle partageait ce quota, une journée chargée en analyses
contextuelles pourrait empêcher un vrai settlement d'aboutir. Elle a donc son
propre fournisseur, et une panne de l'un ne peut pas dégrader l'autre.

  Mistral AI — raisonnement ET recherche web, une seule clé :
    /v1/chat/completions  → complétion simple
    /v1/conversations     → complétion AVEC connecteur `web_search` intégré

POURQUOI PAS BRAVE (décision opérateur 2026-07-23). La spec d'origine
prévoyait Brave Search comme fournisseur de recherche. Le plan gratuit de
Brave (2 000 req/mois) exige une carte bancaire à l'inscription, même sans
débit — l'opérateur n'en a pas. Le connecteur `web_search` de Mistral rend
Brave inutile : vérifié live le 2026-07-23, il retourne de vrais résultats
datés avec URLs sources exploitables, sur la même clé, sans second compte.
Détail amusant et rassurant : le champ `favicon` des résultats pointe vers
`imgs.search.brave.com` — la recherche de Mistral EST propulsée par Brave.
On obtient donc les résultats visés par la spec, sans le compte ni la carte.

Ce que ce choix coûte, honnêtement : Wiz n'a plus de redondance INTERNE (si
Mistral tombe, Wiz est muet, là où Brave+Mistral laissaient un demi-service).
C'est acceptable — Wiz est optionnel par construction, et l'invariant qui
compte (une panne Wiz ne peut pas toucher le moteur) est intact, puisque le
moteur ne dépend ni de Mistral ni des conversations API.

Ce module ne contient AUCUNE logique métier : pas de prompt, pas de scoring,
pas de notion de signal. Il expose le même contrat que core/ai_search.py pour
que les call sites se lisent pareil :

    wiz_available()          MISTRAL_API_KEY présente ?
    wiz_dead()               tous les modèles Mistral morts ce process ?
    search_exhausted()       budget d'analyses du run épuisé ?
    mistral_search(prompt)   -> (texte, [{title,url,description}], modèle)
    mistral_complete(prompt) -> (texte, modèle)   — sans recherche

Distinction critique, reprise telle quelle d'ai_search.py : « je n'ai pas pu
chercher » n'est PAS « l'information n'existe pas ». Un appelant qui écrit un
verdict doit tester wiz_dead()/search_exhausted() avant de conclure — sinon il
transforme une panne de quota en affirmation sur le monde.

Env requis : MISTRAL_API_KEY. Sans elle, tout retourne None/[] silencieusement,
comme ai_search.py sans GROQ_API_KEY : Wiz est optionnel, son absence ne doit
jamais lever.
"""
import json
import logging
import os
import time

import requests

from core.constants import (
    WIZ_MISTRAL_MIN_INTERVAL_S,
    WIZ_SEARCH_RESULTS_MAX,
    wiz_mistral_models,
    wiz_run_budget,
)

log = logging.getLogger("PREDATOR.wiz_ai")

MISTRAL_CHAT_URL = "https://api.mistral.ai/v1/chat/completions"
MISTRAL_CONV_URL = "https://api.mistral.ai/v1/conversations"

# ── État process ──────────────────────────────────────────────────────
# Motif `_tavily_used` d'ai_search.py : un compteur au niveau du process
# protège contre un run pathologique (une boucle qui repart, un lot de
# matchs anormalement gros). Le plafond est relu à chaque appel via
# wiz_run_budget() plutôt que figé à l'import, pour qu'un test ou un run
# manuel puisse le changer par env sans réimporter.
#
# ⚠️ CE COMPTEUR N'EST PAS LA VRAIE LIMITE. Corrigé le 2026-07-23 après le
# premier run en production : j'avais écrit ici que le facteur limitant était
# la durée du run (2 RPM = 31 s/match) et que les tokens de connecteur
# (~6 000 par recherche) n'étaient pas contraignants face au budget mensuel.
# C'était FAUX — le connecteur web_search a un quota PROPRE, non documenté
# dans les chiffres de tokens, et bien plus serré (voir _search_quota_dead).
# Ce compteur reste utile comme garde-fou local, mais c'est Mistral qui
# tranche : quand il répond « web_search rate limit reached », le run
# s'arrête, quel que soit ce budget.
_searches_used = 0

# Modèles Mistral morts ce process. Comme pour Groq, le quota est appliqué
# PAR MODÈLE : marquer un flag global ferait qu'une 429 sur mistral-large
# court-circuiterait mistral-small qui a encore tout son quota.
_mistral_dead_models: set = set()

# Quota du CONNECTEUR web_search épuisé — terminal pour ce process.
#
# Découvert en production le 2026-07-23 (premier run réel, job 30006390396) :
# le connecteur a un quota PROPRE, indépendant de celui des modèles et
# PAR COMPTE (pas par clé — régénérer la clé n'y change rien). La signature
# est sans ambiguïté :
#     GET  /v1/models        -> 200   la clé est parfaitement valide
#     POST /v1/conversations -> 429   {"detail":"web_search rate limit reached."}
#
# Ce message ne contient ni « per day » ni « quota », donc la classification
# générique des 429 le prenait pour une limite par MINUTE et retentait :
# 3 modèles x 3 tentatives x ~31 s de throttle = ~7 minutes brûlées par
# match, pour zéro résultat. Le run écrivait une ligne INDISPONIBLE toutes
# les 6 minutes jusqu'à son timeout global.
#
# Un quota de connecteur ne se rattrape pas en changeant de modèle : il est
# au niveau du compte. Dès qu'on le voit, on coupe tout le run.
_search_quota_dead = False

# Throttle 2 RPM. Initialisé à 0.0 : le premier appel d'un run part
# immédiatement, on ne paie l'attente qu'entre deux appels.
_last_mistral_call = 0.0


def _reset_state() -> None:
    """Remet l'état process à zéro. Réservé aux tests — jamais appelé en prod
    (un run = un process, l'état doit persister d'un match à l'autre)."""
    global _searches_used, _last_mistral_call, _search_quota_dead
    _searches_used = 0
    _last_mistral_call = 0.0
    _search_quota_dead = False
    _mistral_dead_models.clear()


# ══════════════════════════════════════════════════════════════════════
# Disponibilité
# ══════════════════════════════════════════════════════════════════════

def wiz_available() -> bool:
    """True si Wiz peut fonctionner. Sans clé Mistral, tout le module est inerte."""
    return bool(os.environ.get("MISTRAL_API_KEY"))


def search_available() -> bool:
    """True si Wiz peut chercher sur le web.

    Depuis l'abandon de Brave (2026-07-23) c'est la MÊME clé que le
    raisonnement — la fonction est conservée parce que les appelants
    doivent continuer à distinguer les deux capacités : R4 interdit de
    produire une analyse « de mémoire », donc si un jour la recherche
    redevient un fournisseur distinct, le call site n'a pas à changer.
    """
    return wiz_available()


def wiz_dead() -> bool:
    """True quand PLUS AUCUN modèle Mistral n'est utilisable ce process.

    L'appelant qui écrit un verdict s'en sert pour distinguer « le modèle a
    répondu qu'il n'y a rien » de « je n'ai pas pu demander » : le second
    donne INDISPONIBLE, jamais un score fabriqué.
    """
    models = wiz_mistral_models()
    return bool(models) and all(m in _mistral_dead_models for m in models)


def search_exhausted() -> bool:
    """True quand plus aucune recherche n'est possible pour ce process.

    Deux causes, fusionnées parce que l'appelant en tire la même conclusion
    (arrêter le run) : le budget local est atteint, ou Mistral a répondu que
    le quota du connecteur web_search est épuisé côté compte.
    """
    return _search_quota_dead or _searches_used >= wiz_run_budget()


def run_budget_exhausted() -> bool:
    """True quand le budget LOCAL du run est atteint — et lui seul.

    Distinct de search_exhausted(), qui fusionne ce budget avec la mort du
    connecteur Mistral. Cette fusion était juste tant que le connecteur était
    la seule source : les deux causes menaient au même arrêt du run. Depuis
    core/wiz_sources.py elles divergent — un connecteur mort bascule sur
    Google News, alors que le budget local, lui, borne toujours la DURÉE du
    run (2 RPM côté Mistral, timeout-minutes: 20 côté Actions).
    """
    return _searches_used >= wiz_run_budget()


def search_quota_dead() -> bool:
    """True quand c'est le quota du CONNECTEUR (pas le budget local) qui a
    coupé. Permet à run_wiz.py de le dire explicitement dans son log :
    le remède n'est pas le même (attendre / changer de plan, vs baisser
    WIZ_RUN_BUDGET)."""
    return _search_quota_dead


def search_credits_left() -> int:
    """Recherches encore disponibles pour ce run."""
    if _search_quota_dead:
        return 0
    return max(0, wiz_run_budget() - _searches_used)


def queries_used() -> int:
    """Recherches consommées par ce process — pour wiz_analysis.queries_used
    et le log de synthèse du run."""
    return _searches_used


# ══════════════════════════════════════════════════════════════════════
# Throttle
# ══════════════════════════════════════════════════════════════════════

def _throttle() -> None:
    """Respecte les 2 RPM du free tier Mistral.

    Wiz est un batch cron, pas de l'interactif : attendre 31s entre deux
    matchs ne coûte rien de perceptible (le workflow a 20 minutes), alors
    que contourner la limite coûterait des 429 en cascade et un modèle
    marqué mort à tort.
    """
    global _last_mistral_call
    wait = WIZ_MISTRAL_MIN_INTERVAL_S - (time.time() - _last_mistral_call)
    if wait > 0:
        log.debug("Mistral: throttle 2 RPM — attente %.1fs", wait)
        time.sleep(wait)
    _last_mistral_call = time.time()


def _handle_error(r, model: str, label: str) -> str:
    """Classe une réponse HTTP non-200.

    Retourne 'retry', 'dead' (ce modèle est fichu, essayer le suivant),
    'search_dead' (le connecteur est fichu, changer de modèle ne sert à
    rien) ou 'give_up'.
    """
    global _search_quota_dead

    if r.status_code == 429:
        body = r.text[:500].lower()

        # Quota du connecteur web_search — terminal, et au niveau du COMPTE :
        # réessayer avec un autre modèle donne exactement la même 429.
        # Voir le commentaire de _search_quota_dead plus haut pour l'incident
        # qui a rendu cette branche nécessaire.
        if "web_search" in body or "search rate limit" in body:
            _search_quota_dead = True
            log.critical("%s: quota du connecteur web_search épuisé (compte, pas clé) — "
                         "recherche coupée pour ce process | %s", label, r.text[:200])
            return "search_dead"
        # Comme Groq : per-minute = récupérable, per-day/month = terminal
        # pour ce modèle. Le throttle devrait rendre le premier cas rare ;
        # s'il arrive quand même, attendre une fenêtre entière est moins
        # coûteux que de marquer un modèle mort à tort.
        if any(k in body for k in ("per day", "daily", "per month", "monthly", "quota")):
            _mistral_dead_models.add(model)
            log.critical("%s[%s]: quota Mistral épuisé — modèles morts=%s | %s",
                         label, model, sorted(_mistral_dead_models), r.text[:200])
            return "dead"
        return "retry"

    if r.status_code in (401, 403):
        # Clé refusée : aucun modèle ne passera, inutile d'essayer les suivants.
        for m in wiz_mistral_models():
            _mistral_dead_models.add(m)
        log.critical("%s: HTTP %d (clé Mistral refusée) — Wiz coupé pour ce process | %s",
                     label, r.status_code, r.text[:200])
        return "dead"

    if r.status_code in (400, 404, 422):
        # Le plus souvent : nom de modèle inconnu/retiré du free tier, ou
        # connecteur web_search indisponible sur ce plan. On marque CE
        # modèle mort et on laisse l'appelant essayer le suivant.
        _mistral_dead_models.add(model)
        log.error("%s[%s]: HTTP %d — modèle écarté (nom invalide ou connecteur "
                  "indisponible ?) | %s", label, model, r.status_code, r.text[:250])
        return "dead"

    if r.status_code >= 500:
        return "retry"

    log.error("%s[%s]: HTTP %d: %s", label, model, r.status_code, r.text[:300])
    return "give_up"


# ══════════════════════════════════════════════════════════════════════
# Recherche web + raisonnement (un seul appel)
# ══════════════════════════════════════════════════════════════════════

def _parse_conversation(payload: dict) -> tuple[str, list[dict]]:
    """Extrait (texte, sources) d'une réponse /v1/conversations.

    Les sources sont le garde-fou R4 de core/wiz_engine.py : tout argument
    citant une URL absente de ce set est jeté. Le modèle ne contrôle pas ce
    set, il ne peut donc pas l'halluciner.

    Elles arrivent à DEUX endroits distincts, et il faut les deux — leçon
    apprise en conditions réelles le 2026-07-23 :

      1. `tool.execution[].info.result` — les résultats bruts de chaque
         recherche, présents dès qu'une recherche a eu lieu. C'est la
         source de vérité.
      2. `message.output.content[].tool_reference` — les citations inline,
         présentes UNIQUEMENT quand le modèle rédige en prose et cite ses
         sources. Wiz demande du JSON strict, donc ce cas ne se produit
         quasiment jamais : ne lire que ça donnait 0 source à chaque appel,
         alors que la recherche avait bien tourné (2 requêtes facturées).
         Un R4 nourri par un set vide rejette TOUS les arguments et rend
         INDISPONIBLE en boucle — panne silencieuse parfaite, le run est
         vert et ne produit rien.

    On lit donc (1) en priorité et on complète avec (2).
    """
    text_parts: list[str] = []
    sources: list[dict] = []
    seen: set = set()

    def _add(url, title, desc):
        url = (url or "").strip()
        if not url or url in seen:
            return   # une source sans URL ne peut pas étayer un argument
        seen.add(url)
        sources.append({"title": (title or "").strip(),
                        "url": url,
                        "description": (desc or "").strip()})

    for out in payload.get("outputs") or []:
        otype = out.get("type")

        # ── (1) résultats bruts des recherches ────────────────────────
        if otype == "tool.execution":
            raw = (out.get("info") or {}).get("result")
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except (json.JSONDecodeError, ValueError):
                    raw = None
            if isinstance(raw, dict):
                # {"iH6dcaXU": {"url":..., "title":..., "description":...,
                #               "snippets":[...], "source":"brave"}, ...}
                for item in raw.values():
                    if not isinstance(item, dict):
                        continue
                    desc = item.get("description") or ""
                    snippets = item.get("snippets")
                    if isinstance(snippets, list) and snippets:
                        desc = " ".join(str(s) for s in snippets[:3]) or desc
                    _add(item.get("url"), item.get("title"), desc)
            continue

        # ── (2) citations inline, quand il y en a ─────────────────────
        if otype != "message.output":
            continue
        content = out.get("content")
        if isinstance(content, str):
            text_parts.append(content)
            continue
        for chunk in content or []:
            if not isinstance(chunk, dict):
                continue
            if chunk.get("type") == "text":
                text_parts.append(chunk.get("text") or "")
            elif chunk.get("type") == "tool_reference":
                _add(chunk.get("url"), chunk.get("title"), chunk.get("description"))

    return "".join(text_parts), sources[:WIZ_SEARCH_RESULTS_MAX]


def mistral_search(prompt: str, label: str = "WIZ",
                   timeout: int = 90) -> tuple[str | None, list[dict], str | None]:
    """Recherche web + raisonnement en UN appel, via le connecteur web_search.

    Retourne (texte, sources, modèle). Les trois valent None/[]/None si
    aucun modèle n'a répondu — l'appelant écrit alors INDISPONIBLE, jamais
    un verdict.

    Aucun agent n'est créé côté Mistral : on passe `model` + `tools`
    directement à /v1/conversations. Un agent serait un objet persistant sur
    le compte de l'opérateur, à créer/nettoyer, pour zéro bénéfice ici — Wiz
    ne garde aucun état entre deux matchs.

    Le nom du modèle remonte parce qu'il est stocké sur chaque analyse
    (wiz_analysis.model_used) : quand on mesurera la valeur réelle de Wiz au
    Brier score, il faudra pouvoir séparer les analyses par modèle.
    """
    global _searches_used

    api_key = os.environ.get("MISTRAL_API_KEY")
    if not api_key:
        return None, [], None
    if search_exhausted():
        log.warning("Wiz: budget de recherches du run épuisé (%d) — analyse sautée",
                    wiz_run_budget())
        return None, [], None

    headers = {"Authorization": f"Bearer {api_key}",
               "Content-Type":  "application/json",
               "Accept":        "application/json"}

    for model in wiz_mistral_models():
        if model in _mistral_dead_models:
            continue
        payload = {
            "model":  model,
            "tools":  [{"type": "web_search"}],
            "inputs": prompt,
        }

        for attempt in range(3):
            _throttle()
            try:
                r = requests.post(MISTRAL_CONV_URL, json=payload,
                                  headers=headers, timeout=timeout)
            except Exception as e:
                log.warning("%s[%s]: erreur réseau (tentative %d/3): %s",
                            label, model, attempt + 1, e)
                time.sleep(5 * (attempt + 1))
                continue

            if r.status_code != 200:
                verdict = _handle_error(r, model, label)
                if verdict == "search_dead":
                    # Inutile d'essayer les autres modèles : le quota est
                    # au niveau du compte, pas du modèle.
                    return None, [], None
                if verdict == "retry":
                    wait = WIZ_MISTRAL_MIN_INTERVAL_S * (attempt + 1)
                    log.warning("%s[%s]: HTTP %d — nouvelle tentative dans %.0fs",
                                label, model, r.status_code, wait)
                    time.sleep(wait)
                    continue
                break   # modèle mort ou abandon : on passe au suivant

            _searches_used += 1
            try:
                text, sources = _parse_conversation(r.json())
            except Exception as e:
                log.error("%s[%s]: parse réponse: %s", label, model, e)
                break

            log.info("%s[%s]: %d source(s) réelle(s) (%d/%d du budget)",
                     label, model, len(sources), _searches_used, wiz_run_budget())
            return (text or None), sources, model

    return None, [], None


# ══════════════════════════════════════════════════════════════════════
# Complétion simple (sans recherche)
# ══════════════════════════════════════════════════════════════════════

def mistral_complete(prompt: str, label: str = "WIZ",
                     max_tokens: int = 2048, temperature: float = 0.1,
                     timeout: int = 60) -> tuple[str | None, str | None]:
    """Complétion Mistral SANS recherche web, throttlée, avec repli de modèle.

    Conservée bien que le chemin nominal de Wiz passe par mistral_search() :
    elle sert au smoke test ci-dessous (valider un nom de modèle sans
    consommer de tokens de connecteur) et garde la porte ouverte à un usage
    futur qui n'aurait pas besoin de sources.
    """
    api_key = os.environ.get("MISTRAL_API_KEY")
    if not api_key:
        return None, None

    headers = {"Authorization": f"Bearer {api_key}",
               "Content-Type":  "application/json",
               "Accept":        "application/json"}

    for model in wiz_mistral_models():
        if model in _mistral_dead_models:
            continue
        payload = {"model": model, "messages": [{"role": "user", "content": prompt}],
                   "max_tokens": max_tokens, "temperature": temperature}

        for attempt in range(3):
            _throttle()
            try:
                r = requests.post(MISTRAL_CHAT_URL, json=payload,
                                  headers=headers, timeout=timeout)
            except Exception as e:
                log.warning("%s[%s]: erreur réseau (tentative %d/3): %s",
                            label, model, attempt + 1, e)
                time.sleep(5 * (attempt + 1))
                continue

            if r.status_code != 200:
                verdict = _handle_error(r, model, label)
                if verdict == "search_dead":
                    return None, None
                if verdict == "retry":
                    time.sleep(WIZ_MISTRAL_MIN_INTERVAL_S * (attempt + 1))
                    continue
                break

            try:
                text = r.json()["choices"][0]["message"]["content"] or ""
            except Exception as e:
                log.error("%s[%s]: parse réponse: %s", label, model, e)
                break
            if text.strip():
                return text, model
            break

    return None, None


# ══════════════════════════════════════════════════════════════════════
# Smoke test manuel
# ══════════════════════════════════════════════════════════════════════

def selftest() -> int:
    """Vérifie que la clé Mistral répond et que le connecteur web_search
    retourne de vraies sources.

    Existe parce que les noms de modèles n'étaient pas validés à l'écriture
    du module : `python -m core.wiz_ai` est le moyen le plus court de savoir
    si WIZ_MISTRAL_MODELS a besoin d'être corrigé, sans attendre un run cron.
    Retourne un code de sortie shell.
    """
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    if not wiz_available():
        print("MISTRAL_API_KEY absente — rien à tester")
        return 1

    print("── Modèles (complétion simple) ───────────────")
    ok_models = []
    for model in wiz_mistral_models():
        _mistral_dead_models.discard(model)
        saved = list(wiz_mistral_models())
        os.environ["WIZ_MISTRAL_MODELS"] = model     # forcer ce seul modèle
        txt, used = mistral_complete('Réponds exactement: {"ok":true}',
                                     max_tokens=32, temperature=0.0, label="SELFTEST")
        os.environ.pop("WIZ_MISTRAL_MODELS", None)
        print(f"  {model:28s} -> {(txt or 'AUCUNE RÉPONSE')[:50]!r}")
        if txt:
            ok_models.append(model)
        del saved

    print("── Recherche web (connecteur web_search) ─────")
    text, sources, model = mistral_search(
        "Real Madrid team news: injuries or suspensions this week? Cite sources.",
        label="SELFTEST")
    print(f"  modèle utilisé : {model or '—'}")
    print(f"  sources        : {len(sources)}")
    for s in sources[:3]:
        print(f"   - {s['title'][:65]}\n     {s['url']}")

    print("─────────────────────────────────────────────")
    print(f"  modèles OK : {ok_models or 'AUCUN'}")
    print(f"  recherches consommées : {queries_used()}")
    ok = bool(ok_models) and bool(sources)
    print(f"  résultat : {'OK' if ok else 'INCOMPLET'}")
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    sys.exit(selftest())
