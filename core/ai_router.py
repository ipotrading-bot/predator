"""
core/ai_router.py — routeur IA multi-fournisseurs : registre, lanes, bascule.

PRINCIPE DIRECTEUR — À LIRE AVANT DE TOUCHER À CE FICHIER
==========================================================
**Le paysage des paliers gratuits churne CHAQUE MOIS.** Ce n'est pas une
crainte, c'est l'historique de ce projet :

    2026-06   Gemini : grounding gratuit passé à « limit: 0 »
    2026-07-30 GitHub Models : RETIRÉ par GitHub
    2026-08   Cerebras : palier gratuit sans carte fermé

Vérifié en live le 2026-08-22 depuis le runner, et c'est sans appel :

    GET https://models.github.ai/catalog/models
      → HTTP 410 {"code":"github_models_retirement_brownout"}
    GET https://api.cerebras.ai/v1/models
      → HTTP 403 {"detail":"Not authenticated"}

**Conséquence architecturale : un nom de modèle n'est JAMAIS une dépendance
vitale.** Chaque lane déclare une LISTE de préférences ; le routeur retient le
premier modèle qui existe VRAIMENT dans le catalogue publié par le fournisseur
au moment du run. C'est l'architecture — registre, découverte, bascule, alerte
— qui a de la valeur, pas la liste.

LA PANNE QUE CE MODULE EXISTE POUR EMPÊCHER — ELLE ÉTAIT DÉJÀ LÀ
------------------------------------------------------------------
Au moment d'écrire ce module, `core/ai_search.py` portait en dur :

    OPENROUTER_MODEL = "meta-llama/llama-3.3-70b-instruct:free"

Or le catalogue OpenRouter du 2026-08-22 (421 modèles, dont 18 en `:free`)
**ne contient plus ce modèle**. Seule la variante PAYANTE
`meta-llama/llama-3.3-70b-instruct` subsiste. Le repli OpenRouter était donc
mort — et mort EN SILENCE : l'appel partait, le fournisseur rendait une erreur
de modèle inconnu, le code loggait un avertissement et passait au suivant.
Personne n'aurait vu la différence entre « le repli n'a pas servi » et « le
repli ne peut plus servir ».

C'est exactement ce que `refresh_catalogues()` détecte désormais au démarrage
de chaque run, et ce que l'alerte Telegram remonte quand une lane tombe sous
deux fournisseurs sains.

UN COMPTE PAR FOURNISSEUR — RÈGLE NON NÉGOCIABLE
-------------------------------------------------
La capacité vient de la DIVERSITÉ des fournisseurs, jamais de comptes
multiples chez le même. Deux clés d'un même compte partagent le quota (vérifié
sur Groq : le TPD est compté par organisation) et violent les CGU. Les
fournisseurs dont les conditions réservent le palier gratuit à un usage non
commercial ou d'évaluation portent un `terms_flag` et sont exclus des lanes de
production par défaut (voir `PRODUCTION_SAFE`).

CE QUI N'EST PAS ENRÔLÉ, ET POURQUOI
-------------------------------------
  - endpoints anonymes SANS clé (Pollinations, LLM7…) : même défaut fatal que
    les sources sans clé de l'incident du 10→20 août — filtrés par IP depuis
    les runners, et CGU floues. La leçon est acquise, on ne la repaie pas ;
  - multi-comptes d'un même fournisseur : CGU ;
  - fournisseurs exigeant une carte pour un essai expirant (Cerebras,
    Fireworks) : sauf décision opérateur explicite.
"""
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import requests

from core import daily_quota

log = logging.getLogger("PREDATOR.ai_router")

# ── Lanes ────────────────────────────────────────────────────────────
# Chaque appel IA du pipeline DÉCLARE sa lane ; le routeur choisit le
# fournisseur sain le moins cher de cette lane. Une lane est un besoin, pas un
# fournisseur — c'est ce qui permet de remplacer un fournisseur sans toucher à
# un seul call site.
FILTER        = "filter"          # tri rapide de candidats, latence < 1 s
ANALYZE       = "analyze"         # analyse contextuelle profonde
TRANSLATE_CJK = "translate_cjk"   # alias d'équipes CJK (mission 3)
SEARCH_READ   = "search_read"     # recherche / lecture web
SETTLEMENT    = "settlement"      # SACRÉE — voir plus bas
WIZ           = "wiz"             # Mistral seul, domaine de panne isolé

LANES = (FILTER, ANALYZE, TRANSLATE_CJK, SEARCH_READ, SETTLEMENT, WIZ)

# Circuit breaker par fournisseur : 3 échecs consécutifs → 30 min de repos.
BREAKER_THRESHOLD = int(os.environ.get("AI_BREAKER_THRESHOLD", "3"))
BREAKER_REST_MIN  = int(os.environ.get("AI_BREAKER_REST_MIN", "30"))

# Sous ce nombre de fournisseurs sains, une lane est en danger → alerte.
LANE_MIN_HEALTHY = int(os.environ.get("AI_LANE_MIN_HEALTHY", "2"))

CATALOG_TIMEOUT = int(os.environ.get("AI_CATALOG_TIMEOUT", "12"))


@dataclass(frozen=True)
class Provider:
    """Carte d'identité d'un fournisseur. DÉCLARATIF : aucune logique ici.

    `models` est une liste de PRÉFÉRENCES, pas un contrat. Le routeur retient
    le premier modèle effectivement présent au catalogue ; si aucun ne l'est,
    le fournisseur est écarté de la lane avec un log explicite — jamais un
    appel voué à l'échec.
    """
    name: str
    base_url: str                    # racine OpenAI-compatible, sans /chat/completions
    env_key: str
    models: tuple = ()
    lanes: tuple = ()
    rpm: int = 0                     # 0 = non documenté
    daily_requests: int = 0          # budget prudent côté PREDATOR
    daily_tokens: int = 0            # 0 = non documenté / non compté
    terms_flag: str = ""             # "" | "non_commercial" | "evaluation"
    catalog_path: str = "/models"
    headers: dict = field(default_factory=dict)
    note: str = ""

    @property
    def bucket(self) -> str:
        return f"ai_{self.name}"

    @property
    def chat_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/chat/completions"

    @property
    def catalog_url(self) -> str:
        return f"{self.base_url.rstrip('/')}{self.catalog_path}"

    def key(self) -> str | None:
        return os.environ.get(self.env_key) or None


# ── REGISTRE ─────────────────────────────────────────────────────────
# Quotas et modèles CONSTATÉS EN LIVE le 2026-08-22 depuis le runner (voir le
# rapport). `daily_requests` est un budget PRUDENT côté PREDATOR, pas la
# limite du fournisseur : on veut basculer avant de se faire couper, jamais
# après (leçon du compte api-sports SUSPENDU le 2026-08-20).
REGISTRY: tuple = (
    # ── Historique, préférés de leurs lanes actuelles (compat ascendante) ──
    Provider(
        name="groq", base_url="https://api.groq.com/openai/v1",
        env_key="GROQ_API_KEY",
        models=("llama-3.3-70b-versatile", "llama-3.1-8b-instant"),
        lanes=(FILTER, ANALYZE, SETTLEMENT, SEARCH_READ),
        rpm=30, daily_requests=400,
        note="TPD 100k compté PAR ORGANISATION, pas par clé",
    ),
    # ── Palier gratuit permanent, sans carte — priorité 1 ──
    Provider(
        name="openrouter", base_url="https://openrouter.ai/api/v1",
        env_key="OPENROUTER_API_KEY",
        # Préférences vérifiées présentes au catalogue :free du 2026-08-22.
        # Elles PÉRIMERONT — c'est le principe même du module.
        models=("nvidia/nemotron-3-super-120b-a12b:free",
                "z-ai/glm-5.2:free",
                "google/gemma-4-31b-it:free",
                "nvidia/nemotron-3-nano-30b-a3b:free"),
        lanes=(ANALYZE, FILTER, TRANSLATE_CJK),
        rpm=20, daily_requests=150,
        headers={"HTTP-Referer": "https://github.com/predator-paim",
                 "X-Title": "PREDATOR"},
        note="421 modeles, 18 en :free (2026-08-22) ; ~50 req/j, 1000/j apres credit unique",
    ),
    Provider(
        name="nvidia_nim", base_url="https://integrate.api.nvidia.com/v1",
        env_key="NVIDIA_NIM_API_KEY",
        models=("deepseek-ai/deepseek-v4-flash-0731",
                "meta/llama-3.3-70b-instruct"),
        lanes=(ANALYZE, FILTER),
        rpm=40, daily_requests=200, terms_flag="evaluation",
        note="102 modeles au catalogue (2026-08-22) ; statut evaluation a documenter",
    ),
    Provider(
        name="sambanova", base_url="https://api.sambanova.ai/v1",
        env_key="SAMBANOVA_API_KEY",
        models=("Meta-Llama-3.3-70B-Instruct", "DeepSeek-V3.2", "gpt-oss-120b"),
        lanes=(FILTER, ANALYZE, SETTLEMENT),
        rpm=60, daily_requests=200, daily_tokens=200_000,
        note="7 modeles (2026-08-22) ; ~200k tokens/j par modele — verifier si carte requise",
    ),
    Provider(
        name="ovh", base_url="https://oai.endpoints.kepler.ai.cloud.ovh.net/v1",
        env_key="OVH_AI_API_KEY",
        models=("Meta-Llama-3_3-70B-Instruct", "Qwen3-32B",
                "Mistral-Small-3.2-24B-Instruct-2506"),
        lanes=(FILTER, ANALYZE, TRANSLATE_CJK),
        rpm=0, daily_requests=150,
        note="24 modeles (2026-08-22) ; souverainete UE",
    ),
    Provider(
        name="scaleway", base_url="https://api.scaleway.ai/v1",
        env_key="SCALEWAY_API_KEY",
        models=("llama-3.3-70b-instruct", "qwen3-32b"),
        lanes=(FILTER, ANALYZE),
        rpm=0, daily_requests=150,
        note="catalogue derriere cle (401 sans cle) ; souverainete UE",
    ),
    Provider(
        name="ollama_cloud", base_url="https://ollama.com/v1",
        env_key="OLLAMA_API_KEY",
        models=("glm-5.2", "kimi-k3", "gpt-oss:120b", "deepseek-v4-flash:0731"),
        lanes=(SETTLEMENT, ANALYZE, TRANSLATE_CJK),
        rpm=0, daily_requests=100,
        catalog_path="/models",
        note="19 modeles (2026-08-22) ; 1 requete a la fois, sessions 5h — batch de nuit",
    ),
    Provider(
        name="cohere", base_url="https://api.cohere.com/compatibility/v1",
        env_key="COHERE_API_KEY",
        models=("command-r-plus", "command-r"),
        lanes=(ANALYZE,),
        rpm=0, daily_requests=30, terms_flag="non_commercial",
        note="1000 appels/mois ; NON COMMERCIAL explicite — hors production",
    ),
    # ── Asie — priorité 2, excellents en CJK (double emploi mission 3) ──
    Provider(
        name="zhipu", base_url="https://api.z.ai/api/paas/v4",
        env_key="ZHIPU_API_KEY",
        models=("glm-4.7-flash", "glm-4.5-flash"),
        lanes=(TRANSLATE_CJK, FILTER),
        rpm=0, daily_requests=200, terms_flag="non_commercial",
        note="GLM-Flash gratuit ; carve-out recherche/non-commercial",
    ),
    Provider(
        name="modelscope", base_url="https://api-inference.modelscope.cn/v1",
        env_key="MODELSCOPE_API_KEY",
        models=("Qwen/Qwen3-30B-A3B", "ZhipuAI/GLM-4.7-Flash", "Qwen/Qwen3-14B"),
        lanes=(TRANSLATE_CJK, ANALYZE),
        rpm=0, daily_requests=150,
        note="46 modeles dont toute la gamme Qwen3 + GLM-4.7-Flash (2026-08-22)",
    ),
    Provider(
        name="siliconflow", base_url="https://api.siliconflow.cn/v1",
        env_key="SILICONFLOW_API_KEY",
        models=("Qwen/Qwen3-8B", "THUDM/glm-4-9b-chat"),
        lanes=(TRANSLATE_CJK,),
        rpm=0, daily_requests=100,
        note="catalogue derriere cle (401 sans cle)",
    ),
    Provider(
        name="upstage", base_url="https://api.upstage.ai/v1",
        env_key="UPSTAGE_API_KEY",
        models=("solar-pro2", "solar-mini"),
        lanes=(TRANSLATE_CJK,),
        rpm=0, daily_requests=50, terms_flag="evaluation",
        note="credits d'essai ; utile ponctuellement pour le coreen",
    ),
)

# Fournisseurs utilisables en PRODUCTION : ceux dont les conditions ne
# réservent pas le palier gratuit à un usage non commercial ou d'évaluation.
# Les autres restent enrôlés (pour l'expérimentation) mais ne sont jamais
# choisis par `route()` sauf `allow_flagged=True` explicite.
PRODUCTION_SAFE = tuple(p for p in REGISTRY if not p.terms_flag)


def by_name(name: str) -> Provider | None:
    return next((p for p in REGISTRY if p.name == name), None)


def active_providers() -> list:
    """Fournisseurs dont la clé est présente dans l'environnement.

    Clé absente = fournisseur ignoré SILENCIEUSEMENT. Zéro dépendance
    obligatoire, exactement comme les adaptateurs de sources de la mission 3 :
    un déploiement qui ne configure que Groq doit tourner sans un warning.
    """
    return [p for p in REGISTRY if p.key()]


# ── Santé partagée (table `meta`, mécanique daily_quota) ─────────────

_HEALTH_KEY = "ai_health_{name}"


def _db():
    try:
        from core.db import get_db
        return get_db(write=True)
    except Exception as e:
        log.debug("ai_router: pas de base (%s)", e)
        return None


def load_health(name: str) -> dict:
    """État d'un fournisseur, ou un gabarit vide. Ne lève jamais."""
    empty = {"provider": name, "consecutive_errors": 0, "breaker_until": None,
             "last_success": None, "tokens_today": 0, "calls_today": 0,
             "failovers": [], "missing_models": []}
    sb = _db()
    if sb is None:
        return empty
    try:
        row = sb.table("meta").select("value").eq(
            "key", _HEALTH_KEY.format(name=name)).maybe_single().execute()
        if row and row.data and row.data.get("value"):
            return {**empty, **json.loads(row.data["value"])}
    except Exception as e:
        log.debug("ai_router[%s]: lecture santé impossible (%s)", name, e)
    return empty


def save_health(health: dict) -> None:
    sb = _db()
    if sb is None:
        return
    try:
        sb.table("meta").upsert(
            {"key": _HEALTH_KEY.format(name=health["provider"]),
             "value": json.dumps(health, ensure_ascii=False),
             "updated_at": datetime.now(timezone.utc).isoformat()},
            on_conflict="key").execute()
    except Exception as e:
        log.debug("ai_router[%s]: écriture santé impossible (%s)", health.get("provider"), e)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def breaker_open(health: dict, now: datetime | None = None) -> bool:
    """Le disjoncteur est-il ouvert (fournisseur au repos) ?"""
    until = health.get("breaker_until")
    if not until:
        return False
    try:
        stamp = datetime.fromisoformat(str(until).replace("Z", "+00:00"))
    except ValueError:
        return False
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return (now or _now()) < stamp


def record_failure(health: dict, reason: str = "") -> dict:
    """Un échec de plus. Au seuil, le disjoncteur s'ouvre pour BREAKER_REST_MIN.

    Une réponse INUTILISABLE (JSON invalide) compte comme un échec, au même
    titre qu'une erreur HTTP : du point de vue du pipeline, un fournisseur qui
    répond du texte non parsable est en panne — et c'est la panne la plus
    coûteuse, parce qu'elle consomme le quota sans rien produire.
    """
    health = dict(health)
    n = int(health.get("consecutive_errors") or 0) + 1
    health["consecutive_errors"] = n
    if n >= BREAKER_THRESHOLD:
        health["breaker_until"] = (_now() + timedelta(minutes=BREAKER_REST_MIN)).isoformat()
        log.warning("ai_router[%s]: %d échecs consécutifs (%s) — repos %d min",
                    health.get("provider"), n, reason or "?", BREAKER_REST_MIN)
    return health


def record_success(health: dict, tokens: int = 0) -> dict:
    health = dict(health)
    health["consecutive_errors"] = 0
    health["breaker_until"] = None
    health["last_success"] = _now().isoformat()
    health["tokens_today"] = int(health.get("tokens_today") or 0) + max(0, tokens)
    health["calls_today"] = int(health.get("calls_today") or 0) + 1
    return health


# ── Découverte des catalogues ────────────────────────────────────────

_catalog_cache: dict = {}


def fetch_catalog(p: Provider, timeout: int | None = None) -> set:
    """Modèles réellement servis par un fournisseur, maintenant.

    Rend un ensemble VIDE si le catalogue est injoignable — et un ensemble
    vide veut dire « je ne sais pas », pas « aucun modèle ». `resolve_model`
    en tient compte : sans catalogue, on garde la préférence déclarée plutôt
    que d'écarter un fournisseur peut-être sain.
    """
    if p.name in _catalog_cache:
        return _catalog_cache[p.name]
    headers = {"Content-Type": "application/json", **p.headers}
    key = p.key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    try:
        r = requests.get(p.catalog_url, headers=headers,
                         timeout=timeout or CATALOG_TIMEOUT)
        if r.status_code != 200:
            log.info("ai_router[%s]: catalogue HTTP %d — préférences conservées",
                     p.name, r.status_code)
            _catalog_cache[p.name] = set()
            return set()
        body = r.json()
    except Exception as e:
        log.info("ai_router[%s]: catalogue injoignable (%s) — préférences conservées",
                 p.name, e)
        _catalog_cache[p.name] = set()
        return set()

    items = body.get("data") or body.get("models") or []
    ids = set()
    for m in items:
        if isinstance(m, dict):
            mid = m.get("id") or m.get("name") or m.get("model")
            if mid:
                ids.add(str(mid))
        elif isinstance(m, str):
            ids.add(m)
    _catalog_cache[p.name] = ids
    return ids


def resolve_model(p: Provider, catalog: set | None = None) -> tuple:
    """(modèle retenu, a_bascule) pour ce fournisseur.

    Retient la PREMIÈRE préférence effectivement présente au catalogue. Si la
    préférence de tête a disparu, la bascule est LOGGÉE — c'est la panne
    silencieuse que ce module existe pour empêcher (cf. le cas OpenRouter en
    tête de fichier). Sans catalogue lisible, on garde la tête de liste : on
    ne débranche pas un fournisseur parce que son endpoint `/models` est
    momentanément muet.
    """
    if not p.models:
        return None, False
    cat = fetch_catalog(p) if catalog is None else catalog
    if not cat:
        return p.models[0], False
    for i, m in enumerate(p.models):
        if m in cat:
            if i > 0:
                log.warning("ai_router[%s]: modèle préféré %r absent du catalogue "
                            "— bascule sur %r", p.name, p.models[0], m)
            return m, i > 0
    log.error("ai_router[%s]: AUCUNE préférence au catalogue (%s) — fournisseur "
              "écarté ce run", p.name, ", ".join(p.models))
    return None, True


def refresh_catalogues(alert=None) -> dict:
    """À lancer au DÉMARRAGE de chaque run.

    Interroge le catalogue de chaque fournisseur actif, résout le modèle de
    chacun, persiste les bascules, et ALERTE si une lane tombe sous
    LANE_MIN_HEALTHY fournisseurs sains. Rend un rapport lisible.

    `alert` : callable(texte) — injecté pour rester testable sans réseau.
    """
    report = {"providers": {}, "lanes": {}, "alerts": []}
    resolved: dict = {}

    actifs = active_providers()
    if not actifs:
        # AUCUN fournisseur configuré n'est pas une dégradation : c'est un
        # choix de déploiement (mode REPRICE, sandbox, tests), et il est déjà
        # visible sans qu'on le télégraphie. Alerter ici enverrait un message
        # par lane à CHAQUE run — le bruit qui fait qu'on n'ouvre plus les
        # alertes, donc qu'on rate la vraie. On n'alerte que sur une capacité
        # qui EXISTAIT et se dégrade.
        log.info("IA: aucun fournisseur configuré — découverte ignorée")
        report["lanes"] = {lane: [] for lane in LANES}
        return report

    for p in actifs:
        model, switched = resolve_model(p)
        health = load_health(p.name)
        entry = {"model": model, "switched": switched,
                 "breaker_open": breaker_open(health),
                 "terms_flag": p.terms_flag}
        if switched:
            fo = list(health.get("failovers") or [])
            fo.append({"at": _now().isoformat(), "from": p.models[0], "to": model})
            health["failovers"] = fo[-20:]
            health["missing_models"] = [m for m in p.models
                                        if m not in fetch_catalog(p)] or []
            save_health(health)
        report["providers"][p.name] = entry
        if model and not entry["breaker_open"]:
            resolved[p.name] = model

    for lane in LANES:
        healthy = [p.name for p in REGISTRY
                   if lane in p.lanes and p.name in resolved]
        report["lanes"][lane] = healthy
        if lane == WIZ:
            continue          # Wiz est mono-fournisseur PAR CONSTRUCTION
        if len(healthy) < LANE_MIN_HEALTHY:
            msg = (f"⚠️ IA — lane `{lane}` : {len(healthy)} fournisseur(s) sain(s) "
                   f"({', '.join(healthy) or 'aucun'}), minimum {LANE_MIN_HEALTHY}")
            report["alerts"].append(msg)
            log.error(msg)

    if report["alerts"] and alert:
        try:
            alert("*PREDATOR — santé IA*\n" + "\n".join(report["alerts"]))
        except Exception as e:
            log.error("ai_router: alerte impossible (%s)", e)
    return report


# ── Routage ──────────────────────────────────────────────────────────

# RÉSERVE SETTLEMENT — sacrée.
# Le 2026-08-02, le TPD Groq a été épuisé par le scan et le settlement n'a
# plus rien pu régler de la journée : le ledger est resté vide et /performance
# figé. La réserve est donc gardée EN NÉGATIF : les lanes autres que
# SETTLEMENT voient leur budget amputé de la réserve, si bien qu'elles
# s'arrêtent avant d'entamer ce qui lui est dû. Personne ne « prend » la
# réserve : les autres n'y ont simplement jamais accès.
SETTLEMENT_RESERVE = int(os.environ.get("AI_SETTLEMENT_RESERVE", "80"))


def budget_left(p: Provider, lane: str) -> int:
    """Requêtes restantes pour ce fournisseur DANS CETTE LANE."""
    if not p.daily_requests:
        return 1 if lane == SETTLEMENT else 1
    spent = daily_quota.spent(p.bucket)
    ceiling = p.daily_requests
    if lane != SETTLEMENT and SETTLEMENT in p.lanes:
        ceiling = max(0, ceiling - SETTLEMENT_RESERVE)
    return max(0, ceiling - spent)


def lane_providers(lane: str, allow_flagged: bool = False) -> list:
    """Fournisseurs sains de la lane, dans l'ordre de préférence du registre.

    Écartés : clé absente, disjoncteur ouvert, budget épuisé pour cette lane,
    aucun modèle au catalogue, et — sauf `allow_flagged` — ceux dont les
    conditions réservent le gratuit à un usage non commercial/évaluation.
    """
    out = []
    for p in REGISTRY:
        if lane not in p.lanes or not p.key():
            continue
        if p.terms_flag and not allow_flagged:
            continue
        if breaker_open(load_health(p.name)):
            log.debug("ai_router[%s]: disjoncteur ouvert — écarté", p.name)
            continue
        if budget_left(p, lane) <= 0:
            log.info("ai_router[%s]: budget épuisé pour la lane %s — écarté",
                     p.name, lane)
            continue
        model, _ = resolve_model(p)
        if not model:
            continue
        out.append((p, model))
    return out


def _extract(body: dict) -> tuple:
    """(texte, tokens) depuis une réponse OpenAI-compatible."""
    try:
        text = body["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        return None, 0
    tokens = 0
    usage = body.get("usage") or {}
    if isinstance(usage, dict):
        tokens = int(usage.get("total_tokens") or 0)
    return text, tokens


def call_provider(p: Provider, model: str, messages: list, max_tokens: int,
                  temperature: float, timeout: int, label: str) -> str | None:
    """UN appel. Met à jour la santé du fournisseur. Ne lève jamais."""
    health = load_health(p.name)
    try:
        r = requests.post(
            p.chat_url,
            json={"model": model, "messages": messages,
                  "max_tokens": max_tokens, "temperature": temperature},
            headers={"Authorization": f"Bearer {p.key()}",
                     "Content-Type": "application/json", **p.headers},
            timeout=timeout)
        daily_quota.add(p.bucket, 1)
    except Exception as e:
        save_health(record_failure(health, f"exception {e}"))
        log.warning("%s[%s]: %s", label, p.name, e)
        return None

    if r.status_code != 200:
        save_health(record_failure(health, f"HTTP {r.status_code}"))
        log.warning("%s[%s]: HTTP %d: %s", label, p.name, r.status_code, r.text[:200])
        return None
    try:
        text, tokens = _extract(r.json())
    except Exception as e:
        save_health(record_failure(health, f"corps illisible {e}"))
        return None
    if not text or not text.strip():
        # Une réponse vide consomme le quota sans rien produire : c'est un
        # échec, pas un succès silencieux.
        save_health(record_failure(health, "réponse vide"))
        return None
    save_health(record_success(health, tokens))
    log.info("%s[%s/%s]: %d tokens", label, p.name, model, tokens)
    return text


def route(messages: list, lane: str, label: str = "AI",
          max_tokens: int = 2048, temperature: float = 0.1,
          timeout: int = 45, allow_flagged: bool = False,
          validator=None) -> tuple:
    """Route un appel vers le premier fournisseur sain de la lane.

    Rend (texte, nom_du_fournisseur) ou (None, None).

    RÈGLE : un même prompt n'est JAMAIS rejoué sur un second fournisseur si le
    premier a répondu quelque chose de VALIDE — pas de double dépense. En
    revanche, une réponse inutilisable (`validator` rend False : JSON
    invalide, par exemple) compte comme un échec de disjoncteur ET autorise
    le fournisseur suivant : on n'a rien obtenu, il faut bien essayer ailleurs.
    """
    if lane not in LANES:
        raise ValueError(f"lane inconnue: {lane!r} (attendu {LANES})")
    candidates = lane_providers(lane, allow_flagged=allow_flagged)
    if not candidates:
        log.warning("%s: aucun fournisseur sain pour la lane %s", label, lane)
        return None, None

    for p, model in candidates:
        text = call_provider(p, model, messages, max_tokens, temperature,
                             timeout, label)
        if text is None:
            continue
        if validator is not None:
            try:
                ok = bool(validator(text))
            except Exception:
                ok = False
            if not ok:
                # Réponse reçue mais inexploitable : compte comme panne.
                save_health(record_failure(load_health(p.name), "réponse invalide"))
                log.warning("%s[%s]: réponse invalide — fournisseur suivant",
                            label, p.name)
                continue
        return text, p.name
    return None, None


def complete(prompt: str, lane: str, label: str = "AI", max_tokens: int = 2048,
             temperature: float = 0.1, timeout: int = 45,
             allow_flagged: bool = False, validator=None) -> tuple:
    """Complétion routée, CACHE D'ABORD.

    Le cache de la mission 2 s'applique AVANT tout appel, quel que soit le
    fournisseur — c'est souvent plus rentable que de la capacité en plus.
    """
    from core.ai_search import _cache_get, _cache_key, _cache_put
    ck = _cache_key(prompt, None)
    hit = _cache_get(ck)
    if hit:
        log.info("%s: cache IA — aucun appel (lane %s)", label, lane)
        return hit, "cache"
    text, provider = route([{"role": "user", "content": prompt}], lane, label,
                           max_tokens, temperature, timeout, allow_flagged, validator)
    if text:
        _cache_put(ck, text)
    return text, provider


def health_summary() -> list:
    """Santé de tous les fournisseurs actifs — pour le rapport hebdo."""
    out = []
    for p in active_providers():
        h = load_health(p.name)
        out.append({
            "provider": p.name,
            "terms_flag": p.terms_flag or "-",
            "lanes": list(p.lanes),
            "calls_today": int(h.get("calls_today") or 0),
            "tokens_today": int(h.get("tokens_today") or 0),
            "budget": p.daily_requests,
            "consecutive_errors": int(h.get("consecutive_errors") or 0),
            "breaker_open": breaker_open(h),
            "last_success": h.get("last_success"),
            "failovers": len(h.get("failovers") or []),
        })
    return out


def probe() -> tuple:
    """(au moins un fournisseur actif ?, détail) — pour scripts/ops.py."""
    act = active_providers()
    if not act:
        return False, "aucun fournisseur configuré"
    bits = []
    for p in act:
        model, switched = resolve_model(p)
        bits.append(f"{p.name}={model or 'AUCUN MODELE'}{' (bascule)' if switched else ''}")
    return True, " | ".join(bits)
