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
     extraction par le modèle de génération Groq en tête du registre.

Les appels SANS recherche (estimateur Tier 3) vont directement sur les
modèles de génération Groq, DANS L'ORDRE DU REGISTRE — ce module n'en
code plus aucun en dur (cf. `_groq_models`, corrigé le 2026-08-26).

Env requis : GROQ_API_KEY (gsk_...) ; optionnel : TAVILY_API_KEY (tvly-...)
pour le fallback, et GROQ_API_KEY_2 / GROQ_API_KEY_3 pour la rotation quand
le quota journalier d'un compte est épuisé (voir _groq_keys). Sans aucune
clé Groq, tout retourne None/[] silencieusement (même dégradation que
l'ancien `if not api_key: return []`).
"""
import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime, timezone

import requests

from core import daily_quota

log = logging.getLogger("PREDATOR.ai_search")

# ── Mission 4 (2026-08-22) : la chaîne de repli est passée au ROUTEUR ──
# Ce module ne tient plus sa propre liste de fournisseurs. Elle vivait ici
# depuis la mission 2 et portait deux fournisseurs MORTS et un modèle MORT :
#
#   Cerebras      → palier gratuit sans carte fermé (août 2026)
#                   GET /v1/models = HTTP 403 "Not authenticated"
#   GitHub Models → RETIRÉ par GitHub le 2026-07-30
#                   GET /catalog/models = HTTP 410 "github_models_retirement_brownout"
#   OpenRouter    → le modèle codé en dur ici,
#                   "meta-llama/llama-3.3-70b-instruct:free", a disparu du
#                   catalogue :free — seule la variante PAYANTE subsiste.
#
# Les trois échouaient EN SILENCE : un warning par appel, et un repli qui ne
# repliait plus rien. C'est précisément la panne que `core/ai_router.py`
# existe pour empêcher — il découvre les catalogues au démarrage du run,
# bascule de modèle, et alerte quand une lane tombe sous deux fournisseurs
# sains. Le registre, les quotas, les disjoncteurs et les lanes sont là-bas.
#
# Ce qui NE change PAS (compat ascendante, voulue) : Groq reste le préféré de
# ses lanes avec sa mécanique de quota par-modèle/par-clé ci-dessous, et
# Tavily reste l'étage 2 de la recherche. Mistral, lui, est passé DANS le
# registre le 2026-08-26 avec la suppression de Wiz : il n'est plus un
# domaine de panne isolé, c'est un fournisseur des lanes de signaux.

# Cache de réponses (même requête normalisée, fenêtre 30 min) dans meta : le
# même slate ne doit jamais être recherché deux fois dans la même fenêtre —
# souvent plus rentable que de la capacité en plus. Sans Supabase : pas de
# cache, on appelle (même dégradation que daily_quota).
AI_CACHE_TTL_MIN = int(os.environ.get("AI_CACHE_TTL_MIN", "30"))

# ── Les modèles Groq se DÉRIVENT du registre, ils ne se recopient pas ──
#
# Ce fichier en portait TROIS copies à la main (`_TIER_MODELS`,
# `_EXTRACT_MODELS`, `_ALL_MODELS`). Le 2026-08-26, `llama-3.3-70b-versatile`
# et `llama-3.1-8b-instant` ont disparu du catalogue Groq : `core/ai_router.py`
# a écarté Groq proprement, avec un log — mais ces trois copies, elles,
# appelaient le modèle mort EN DIRECT, sans passer par le routeur. Résultat :
# des 404 en boucle, retries et backoff, jusqu'au timeout global de 540 s qui
# tuait le Deep Scan du matin (runs 32936332791 et 32814980496).
#
# C'est la panne « listes qui divergent » (INCIDENTS.md), dans sa forme la plus
# coûteuse : le gardien du registre faisait son travail pendant que le vrai
# chemin d'appel l'ignorait. Une liste qui existe déjà ailleurs se dérive.
#
# Import PARESSEUX : `core/ai_router.py` importe ce module (le cache de
# `route()`), un import de module à module créerait un cycle.
def _groq_models() -> list:
    """Préférences Groq, dans l'ordre du registre (source unique de vérité)."""
    from core import ai_router
    p = ai_router.by_name("groq")
    return list(p.models) if p else []


def _tier_models(tier: str) -> list:
    """Modèles Groq à essayer pour ce palier.

    Le palier ne réordonne PLUS la liste. Historiquement « light » mettait le
    petit modèle (8b) devant le gros (70b) pour filtrer vite et pas cher. Le
    catalogue Groq du 2026-08-26 ne le permet plus : le seul modèle qui rende
    quelque chose sous les plafonds serrés du filtrage (max_tokens=80) est la
    tête de liste du registre — les gpt-oss, modèles de raisonnement, rendent
    un contenu VIDE tant qu'on ne leur laisse pas ~200 tokens. Inverser
    l'ordre pour « light » ferait donc échouer silencieusement l'estimateur et
    le dictionnaire d'alias, qui sont précisément ses deux appelants.
    `tier` garde tout son sens ailleurs : il choisit la LANE du repli
    (cf. `_TIER_LANE`).
    """
    return _groq_models()


GROQ_URL   = "https://api.groq.com/openai/v1/chat/completions"
TAVILY_URL = "https://api.tavily.com/search"

# Toujours présent au catalogue Groq — revérifié par GET /models le 2026-08-26,
# et par une inférence réelle le même jour (il a répondu, 511 tokens : sa
# recherche web coûte cher, d'où la réserve settlement).
_SEARCH_MODEL = "groq/compound-mini"       # recherche web intégrée

# Budget Tavily par process — protège le quota mensuel contre un run
# pathologique. Chaque search basic = 1 crédit.
_TAVILY_RUN_BUDGET = int(os.environ.get("TAVILY_RUN_BUDGET", "25"))
_tavily_used = 0

# ── LE BUDGET DE RUN NE PROTÉGEAIT RIEN DU TOUT (2026-08-27) ──────────
# `_tavily_used` est un global de MODULE : il repart à zéro à chaque
# processus. Le plan gratuit fait 1 000 crédits par MOIS. Ce dépôt lance
# ~40 runs par jour (scans, audits, closing line, rapports) et chacun
# s'autorisait 25 crédits : jusqu'à 1 000 crédits par JOUR contre un plan
# MENSUEL de 1 000. Le mois entier pouvait partir en une journée.
#
# Symptôme observé le 2026-08-27 à 20:06 : « Tavily: plafond de PLAN atteint
# (HTTP 432) ». Ce n'était pas un pic d'usage, c'était l'état permanent.
# Conséquence en cascade : Tavily est l'ÉTAGE 2 de la recherche de prix
# Pinnacle. L'étage 1 (groq/compound-mini) meurt sur son quota de tokens
# journalier vers 18:10 ; les deux étages morts ensemble donnent
# « Pinnacle/Search: 0/25 prices received », donc 141 refus « Échec prix
# Sharp » sur 5 runs — le premier motif de rejet du pipeline, très loin
# devant toutes les gardes d'edge réunies (0 refus PLAFOND, 0 SUSPECT).
#
# C'est exactement la panne que core/daily_quota.py existe pour empêcher, et
# Tavily n'y avait jamais été câblé — son docstring cite api-sports et
# odds-api.io, pas lui. Le compteur est désormais PARTAGÉ entre les runs et
# ÉTALÉ sur la journée, comme api-sports.
_TAVILY_BUCKET = "tavily"

# 1 000 crédits/mois. On garde une marge : un plan épuisé le 20 du mois
# laisse le settlement sans filet pendant dix jours, et un résultat de match
# manqué sort une ligne du ledger en `expired` — elle n'apprend plus rien.
_TAVILY_MONTHLY_BUDGET = int(os.environ.get("TAVILY_MONTHLY_BUDGET", "900"))
_TAVILY_DAILY_BUDGET = int(os.environ.get(
    "TAVILY_DAILY_BUDGET", str(max(1, _TAVILY_MONTHLY_BUDGET // 31))))

# Plancher du rythme : ce qu'un seul run doit pouvoir dépenser même à 00h05,
# sinon la source est morte jusqu'à midi.
_TAVILY_CYCLE = int(os.environ.get("TAVILY_CYCLE_COST", "6"))


# Réserve du SETTLEMENT, tenue EN NÉGATIF — même remède que api-sports
# (RESULTS_RESERVE) et que le cloisonnement Groq du 2026-08-02. Les SCANS
# sont amputés, la réserve n'est jamais partagée : un scan de plus vaut moins
# qu'un résultat de moins. Un signal dont le score n'a pas pu être cherché
# sort du ledger en `expired` et n'apprend plus rien à personne.
_TAVILY_RESULTS_RESERVE = int(os.environ.get("TAVILY_RESULTS_RESERVE", "8"))
_priorite_settlement = False


def prioriser_settlement() -> None:
    """Ce process a le droit d'entamer la réserve (appelé par run_audit.py).

    Un drapeau de PROCESS et non une variable d'environnement : l'env est
    posé par les workflows, et un scan qui hériterait par erreur du drapeau
    mangerait silencieusement la réserve — la panne exacte qu'on répare.
    """
    global _priorite_settlement
    _priorite_settlement = True


# ── ÉTAGE 1 : LE QUOTA GROQ SUBISSAIT LA MÊME PANNE ───────────────────
# Le TPD Groq (100 000 tokens/jour, compté PAR ORGANISATION) n'était borné
# par rien côté client : chaque run tapait jusqu'à l'échec. Mesuré le
# 2026-08-27, les DEUX organisations étaient à sec dès 18:10 —
# « Used 98522, Requested 3426 » — et les scans du soir repartaient sans
# étage 1 ET sans étage 2, le plan Tavily étant lui aussi épuisé.
#
# Un appel compound-mini du lot Pinnacle coûte ~3 400 tokens : environ 29
# appels par organisation et par jour. Le compteur ci-dessous est en APPELS,
# pas en tokens — le client ne connaît pas le coût avant de payer, et un
# compteur qu'on ne peut pas tenir honnêtement ne vaut rien.
#
# ⚠️ Budget épuisé ne veut PAS dire abandon : on saute l'étage 1 et on tombe
# sur l'étage 2 (Tavily), qui a son propre budget. C'est tout l'intérêt
# d'avoir deux étages, et c'est ce qui manquait — les deux mouraient ensemble
# parce qu'aucun des deux n'était rationné.
_GROQ_SEARCH_BUCKET = "groq_search"
_GROQ_SEARCH_DAILY = int(os.environ.get("GROQ_SEARCH_DAILY_BUDGET", "110"))
_GROQ_SEARCH_RESERVE = int(os.environ.get("GROQ_SEARCH_RESERVE", "25"))
_GROQ_SEARCH_CYCLE = int(os.environ.get("GROQ_SEARCH_CYCLE_COST", "8"))


def _groq_search_budget_du_jour() -> int:
    """Appels compound-mini encore ouverts aujourd'hui, tous runs confondus."""
    plafond = _GROQ_SEARCH_DAILY
    if not _priorite_settlement:
        plafond = max(1, plafond - _GROQ_SEARCH_RESERVE)
    ouvert = daily_quota.paced_allowance(plafond, _GROQ_SEARCH_CYCLE)
    return max(0, ouvert - daily_quota.spent(_GROQ_SEARCH_BUCKET))


def _tavily_budget_du_jour() -> int:
    """Crédits encore ouverts AUJOURD'HUI, tous runs confondus.

    Sans base (tests, sandbox, panne réseau), `daily_quota` rend 0 dépensé :
    on retombe alors sur le seul budget de run, comme avant. Une source ne
    doit jamais tomber parce que son compteur est muet.
    """
    plafond = _TAVILY_DAILY_BUDGET
    if not _priorite_settlement:
        plafond = max(1, plafond - _TAVILY_RESULTS_RESERVE)
    ouvert = daily_quota.paced_allowance(plafond, _TAVILY_CYCLE)
    return max(0, ouvert - daily_quota.spent(_TAVILY_BUCKET))

# Plafond de PLAN atteint (HTTP 432) — distinct du budget de run ci-dessus.
#
# Mesuré le 2026-08-26 : Tavily rendait 432 « exceeds your plan's set usage
# limit » à CHAQUE appel, 11 fois par scan et 25+ par audit. Sans mémoire, on
# repayait l'aller-retour à chaque requête d'un run pour un refus certain.
#
# Ce n'est pas qu'une question de latence : `search_exhausted()` est ce que
# `core/audit_engine.py` teste AVANT d'écrire un état TERMINAL. Tant que le
# drapeau n'était pas levé, il fallait brûler les 25 crédits du budget de run
# pour que la fonction dise enfin la vérité — 25 refus avant que le settlement
# comprenne qu'il n'avait pas pu chercher.
#
# SEUL le 432 verrouille. Un 429 est un débit par minute : il se repasse.
_tavily_plan_dead = False

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
# modèle de génération qui l'exécute (la tête de liste du registre). Quand le
# corps d'erreur nomme un autre modèle que celui demandé, les DEUX sont
# marqués morts.
_groq_dead_models: dict[int, set] = {}

# Rate-limit MINUTE — cooldown PAR CLÉ, sans jamais dormir dans le moteur.
#
# Mesuré le 2026-08-26 (run Guerrilla 32990495899) : une 4e organisation
# Groq, neuve, a été ajoutée. Elle répondait — mais à la limite par minute
# du palier gratuit, et l'ancien code s'endormait 20 s puis 40 s à CHAQUE
# 429-minute, pour chaque recherche Oracle. Les attentes se sont empilées
# jusqu'au timeout global de 540 s : exit 1, ZÉRO signal persisté. Le run
# d'avant, avec trois clés mortes, abandonnait vite et en émettait 12.
# Une clé vivante mais bridée faisait donc PIRE qu'une clé morte.
#
# Désormais un 429-minute met la clé en cooldown (durée lue dans la réponse
# Groq, bornée) et rend la main TOUT DE SUITE : `_groq_post` passe à la clé
# suivante, et les appels ultérieurs du même run retrouvent la clé une fois
# le délai passé. Le moteur ne dort plus ; c'est le run qui avance.
# Seule exception : un délai annoncé ≤ 3 s se paie sur place.
_groq_cooldown_until: dict[int, float] = {}
_COOLDOWN_MIN_S = 5.0
_COOLDOWN_MAX_S = 60.0
_COOLDOWN_INLINE_S = 3.0        # en dessous, on attend sur place


def _cooling(key_idx: int) -> bool:
    return time.monotonic() < _groq_cooldown_until.get(key_idx, 0.0)


def _retry_after_s(r) -> float:
    """Délai demandé par Groq, en secondes. En-tête Retry-After d'abord,
    sinon le « try again in 12.3s » du corps ; défaut 20 s si rien."""
    try:
        h = r.headers.get("retry-after") if getattr(r, "headers", None) else None
        if h:
            return float(h)
    except (TypeError, ValueError):
        pass
    m = re.search(r"try again in\s*(?:(\d+)m)?\s*([\d.]+)s", r.text or "", re.I)
    if m:
        return float(m.group(1) or 0) * 60 + float(m.group(2))
    return 20.0


def _all_models() -> list:
    """Tout ce que ce module peut demander à Groq — recherche + génération.

    Dérivé du registre (cf. `_groq_models`) : c'est la liste que consultent
    `ai_dead()` et `_mark_dead()`, et une copie périmée ici ferait conclure
    « tout est mort » sur des modèles qui n'existent plus, ou l'inverse.
    """
    return [_SEARCH_MODEL] + _groq_models()

# Le TPD Groq (100k tokens/jour) est compté PAR ORGANISATION : la seule façon
# d'élargir la réserve en gratuit est d'ajouter une clé d'un AUTRE compte.
# Depuis le cloisonnement du 2026-08-02, ces variables ne sont plus servies
# uniformément : audit.yml ne reçoit que la clé du settlement (sous le nom
# GROQ_API_KEY), les workflows de scan reçoivent les autres. C'est le workflow
# qui décide de la réserve, pas ce module — d'où l'ordre simple ci-dessous.
# _5 ajoutée le 2026-08-26 : 4e organisation de SCAN (vérifiée neuve — compound-mini
# y répond 200 pendant que les trois autres orgs rendent 429 TPD). Chaque org
# distincte = +100k tokens/jour de recherche web, la seule ressource que rien
# d'autre ne remplace. Une clé de plus sur une org EXISTANTE ne compterait pas.
_KEY_ENVS = ("GROQ_API_KEY", "GROQ_API_KEY_2", "GROQ_API_KEY_3", "GROQ_API_KEY_4",
             "GROQ_API_KEY_5")


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
        all(m in _dead(i) for m in _all_models())
        for i in range(len(keys))
    )


def _mark_dead(key_idx: int, model: str, body: str) -> None:
    """Marque `model` mort sur CETTE clé + tout modèle connu nommé dans l'erreur."""
    dead = _dead(key_idx)
    dead.add(model)
    for known in _all_models():
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
    return (_tavily_plan_dead
            or _tavily_used >= _TAVILY_RUN_BUDGET
            or _tavily_budget_du_jour() <= 0)


def search_credits_left() -> int:
    """Crédits Tavily encore disponibles pour ce process.

    Permet à un appelant de RÉSERVER ce qui reste au travail le plus
    important : dans core/audit_engine.py, le settlement (résultat réel
    WIN/LOSS, permanent) prime toujours sur la CLV (une métrique).
    """
    if _tavily_plan_dead:
        return 0
    return max(0, min(_TAVILY_RUN_BUDGET - _tavily_used, _tavily_budget_du_jour()))


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
        if model in _dead(idx) or _cooling(idx):
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
            wait = _retry_after_s(r)
            if wait <= _COOLDOWN_INLINE_S:
                log.info("%s[%s]: rate limit minute (clé #%d) — %.1fs, on attend sur place",
                         label, model, key_idx + 1, wait)
                time.sleep(wait)
                continue
            wait = min(max(wait, _COOLDOWN_MIN_S), _COOLDOWN_MAX_S)
            _groq_cooldown_until[key_idx] = time.monotonic() + wait
            log.warning("%s[%s]: rate limit minute (clé #%d) — cooldown %.0fs, "
                        "clé suivante sans attendre", label, model, key_idx + 1, wait)
            return None, True

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
    if _tavily_plan_dead:
        return []                       # plafond de plan : inutile de redemander
    if _tavily_used >= _TAVILY_RUN_BUDGET:
        log.warning("Tavily: budget du run épuisé (%d) — recherche sautée", _TAVILY_RUN_BUDGET)
        return []
    if _tavily_budget_du_jour() <= 0:
        log.warning("Tavily: budget PARTAGÉ du jour épuisé (%d/%d dépensés, plan "
                    "mensuel %d) — recherche sautée. Le reste est gardé pour les "
                    "runs plus tardifs et pour le settlement.",
                    daily_quota.spent(_TAVILY_BUCKET), _TAVILY_DAILY_BUDGET,
                    _TAVILY_MONTHLY_BUDGET)
        return []
    try:
        r = requests.post(TAVILY_URL, json={
            "api_key":     api_key,
            "query":       query,
            "max_results": max_results,
            "search_depth": "basic",
        }, timeout=20)
        _tavily_used += 1
        daily_quota.add(_TAVILY_BUCKET, 1)
        if r.status_code == 432:
            globals()["_tavily_plan_dead"] = True
            log.warning("Tavily: plafond de PLAN atteint (HTTP 432) — plus aucune "
                        "recherche Tavily de ce run : %s", r.text[:160])
            return []
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
    """Fournisseurs de repli configurés (clé présente), hors Groq.

    Délègue au registre du routeur : un fournisseur ajouté là-bas est
    disponible ici sans toucher à ce fichier — c'était tout l'objet de la
    mission 4.
    """
    from core import ai_router
    return [p.name for p in ai_router.active_providers() if p.name != "groq"]


def _fallback_post(messages: list, max_tokens: int, temperature: float,
                   timeout: int, label: str, lane: str = "analyze") -> str | None:
    """Repli derrière Groq — entièrement délégué à `core/ai_router.py`.

    Le routeur applique, pour la lane demandée : clé absente → fournisseur
    ignoré ; disjoncteur ouvert → écarté ; budget journalier atteint →
    écarté ; modèle préféré disparu → bascule loggée. Rend None si aucun
    fournisseur sain ne répond — le caller dégrade comme avant.
    """
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
# fait déclare sa lane explicitement — c'est le cas du settlement (lane
# sacrée) et du dictionnaire d'alias (lane CJK).
_TIER_LANE = {"light": "filter", "heavy": "analyze"}


def ai_complete(prompt: str, label: str = "AI",
                max_tokens: int = 2048, temperature: float = 0.1,
                timeout: int = 45, tier: str = "heavy",
                lane: str | None = None) -> str | None:
    """Complétion SANS recherche web (connaissance interne du modèle).
    `tier` : "heavy" (70b d'abord — settlement, candidats filtrés) ou
    "light" (8b d'abord — filtrage, estimateur). `lane` : lane du routeur
    pour le repli (défaut déduit du palier). Chaîne : Groq (par palier)
    → routeur → None. Cache 30 min sur la requête normalisée."""
    ck = _cache_key(prompt, None)
    hit = _cache_get(ck)
    if hit:
        log.info("%s: cache IA (fenêtre %d min) — aucun appel", label, AI_CACHE_TTL_MIN)
        return hit
    messages = [{"role": "user", "content": prompt}]

    # ── LE ROUTEUR D'ABORD, GROQ EN RÉSERVE ──────────────────────────
    # Ordre INVERSÉ le 2026-08-22, et c'est le changement qui fait que la
    # capacité ajoutée sert réellement.
    #
    # Avant : on épuisait TOUS les modèles Groq avant de regarder ailleurs.
    # Comme Groq répond presque toujours, les autres fournisseurs n'étaient
    # jamais appelés : 100 % de la charge tapait dans les 100 000 tokens/jour
    # de Groq (comptés PAR ORGANISATION), pendant que ~540 requêtes/jour de
    # capacité restaient intactes. On avait ajouté des fournisseurs sans
    # ajouter un seul appel.
    #
    # Pourquoi Groq mérite d'être gardé en réserve plutôt qu'un autre : il est
    # le SEUL du registre à porter `groq/compound-mini`, c'est-à-dire la
    # recherche web intégrée. Aucun autre fournisseur ne la remplace. Chaque
    # token de Groq dépensé ici en complétion simple est un token retiré à
    # `ai_search_complete()` — et c'est exactement le manque qui a bloqué le
    # settlement toute la journée du 2026-08-02.
    #
    # `ai_search_complete()` garde donc l'ordre inverse (compound-mini
    # d'abord) : là, Groq est irremplaçable.
    text = _fallback_post(messages, max_tokens, temperature, timeout, label,
                          lane or _TIER_LANE.get(tier, "analyze"))
    if not text:
        for model in _tier_models(tier):
            text = _groq_post(model, messages, max_tokens, temperature, timeout, label)
            if text:
                break
    if text:
        _cache_put(ck, text)
    return text


def ai_search_complete(prompt: str, queries: list[str], label: str = "AI",
                       max_tokens: int = 2048, temperature: float = 0.1,
                       timeout: int = 60, lane: str | None = None) -> str | None:
    """
    Complétion AVEC recherche web — remplaçant direct de
    Gemini + "tools":[{"google_search":{}}].

    Étage 1 : groq/compound-mini (recherche intégrée, gratuit).
    Étage 2 : Tavily (queries) + extraction llama-3.3-70b.
    Retourne le texte brut (le caller extrait/parse le JSON) ou None.
    """
    ck = _cache_key(prompt, queries)
    hit = _cache_get(ck)
    if hit:
        log.info("%s: cache IA (fenêtre %d min) — aucune recherche", label, AI_CACHE_TTL_MIN)
        return hit

    # ── Étage 1 : compound-mini fait sa propre recherche ──────────────
    messages = [{"role": "user", "content": prompt}]
    if _groq_search_budget_du_jour() <= 0:
        log.info("%s: budget PARTAGÉ compound-mini épuisé pour aujourd'hui "
                 "(%d/%d) — étage 1 sauté, on passe à Tavily. Le reste est "
                 "gardé pour les runs plus tardifs et le settlement.",
                 label, daily_quota.spent(_GROQ_SEARCH_BUCKET), _GROQ_SEARCH_DAILY)
        text = None
    else:
        daily_quota.add(_GROQ_SEARCH_BUCKET, 1)
        text = _groq_post(_SEARCH_MODEL, messages, max_tokens, temperature,
                          timeout, label)
    if text and text.strip():
        _cache_put(ck, text)
        return text

    # compound-mini mort ne veut PAS dire abandon : son quota est celui du
    # 70b, alors que l'étage 2 peut encore tourner sur llama-3.1-8b-instant —
    # ou sur un fournisseur de repli (Mission 2, Phase 3).
    if ai_dead() and not providers_available():
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
    for model in _groq_models():
        text = _groq_post(model, messages, max_tokens, temperature, timeout, label)
        if text:
            _cache_put(ck, text)
            return text
    text = _fallback_post(messages, max_tokens, temperature, timeout, label,
                          lane or "search_read")
    if text:
        _cache_put(ck, text)
    return text
