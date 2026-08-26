"""
core/ai_router.py — routeur IA multi-fournisseurs : registre, lanes, bascule.

PRINCIPE DIRECTEUR — À LIRE AVANT DE TOUCHER À CE FICHIER
==========================================================
**Le paysage des paliers gratuits churne CHAQUE MOIS.** Ce n'est pas une
crainte, c'est l'historique de ce projet :

    2026-06   Gemini : grounding gratuit passé à « limit: 0 »
    2026-07-30 GitHub Models : RETIRÉ par GitHub
    2026-08   Cerebras : palier gratuit sans carte fermé

Vérifié en live le 2026-08-22 depuis le runner :

    GET https://models.github.ai/catalog/models
      → HTTP 410 {"code":"github_models_retirement_brownout"}
    Le corps NOMME le retrait : preuve directe, GitHub Models est sorti du registre.

⚠️ CORRECTION DU 2026-08-22 (même jour) — CERBRAS N'EST PAS MORT.
Le premier passage concluait à sa mort sur un `GET /v1/models` → HTTP 403
`{"detail":"Not authenticated"}`. **Cette lecture était fausse** : 403 sans
clé signifie seulement « endpoint authentifié par clé », exactement comme
Scaleway, Cohere ou Zhipu qui rendent 401 dans les mêmes conditions. Re-testé
avec une clé délibérément invalide :

    GET  /v1/models            + Bearer bogus → 401 {"code":"wrong_api_key"}
    POST /v1/chat/completions  + Bearer bogus → 401 {"code":"wrong_api_key"}

Une API qui sait dire « mauvaise clé » est vivante. Cerebras est donc RÉTABLI
au registre. La leçon vaut plus que le fournisseur : **un 401/403 sans clé ne
prouve jamais qu'un palier gratuit a fermé** — il faut une clé invalide pour
distinguer « authentifié par clé » de « porte close », et un 410 ou un message
explicite pour conclure à un retrait.

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
import re
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
    # Chemin d'inférence. Cloudflare sert son catalogue sous `/ai/models/search`
    # et son endpoint OpenAI sous `/ai/v1/chat/completions` : les deux ne
    # partagent pas le même préfixe, d'où ce champ plutôt qu'un cas particulier.
    chat_path: str = "/chat/completions"
    headers: dict = field(default_factory=dict)
    note: str = ""

    @property
    def bucket(self) -> str:
        return f"ai_{self.name}"

    @property
    def resolved_base(self) -> str:
        """base_url avec ses ${VARIABLES} d'environnement substituées.

        Cloudflare Workers AI expose son endpoint OpenAI-compatible sous
        `/accounts/{account_id}/ai/v1` : l'identifiant de compte fait partie
        de l'URL, pas des en-têtes. Plutôt qu'un cas particulier dans le
        client, le registre porte un gabarit.
        """
        url = self.base_url
        for var in re.findall(r"\$\{([A-Z0-9_]+)\}", url):
            val = os.environ.get(var)
            if val:
                url = url.replace("${" + var + "}", val)
            # Variable absente : on LAISSE le gabarit en place. Le remplacer
            # par une chaîne vide fabriquerait une URL d'apparence valide
            # (`/accounts//ai/v1`) qui échouerait plus tard avec un message
            # réseau incompréhensible, au lieu d'être écartée proprement ici.
        return url

    @property
    def chat_url(self) -> str:
        return f"{self.resolved_base.rstrip('/')}{self.chat_path}"

    @property
    def catalog_url(self) -> str:
        return f"{self.resolved_base.rstrip('/')}{self.catalog_path}"

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
        # Ordre établi par INFÉRENCE RÉELLE le 2026-08-26, aux plafonds SERRÉS
        # de ce pipeline (max_tokens=80 pour un alias, 300 pour l'oracle) —
        # même règle que Gemini et OpenRouter ci-dessous : les instruct
        # d'abord, jamais un modèle de raisonnement en tête.
        #
        # CE JOUR-LÀ, `llama-3.3-70b-versatile` ET `llama-3.1-8b-instant` ont
        # disparu du catalogue Groq (14 modèles, aucun llama de génération) :
        # le routeur écartait donc Groq à chaque run — « AUCUNE préférence au
        # catalogue » — et le pipeline perdait ses lanes FILTER/ANALYZE/
        # SETTLEMENT/SEARCH_READ sans que rien ne soit cassé.
        #
        # Mesuré sur « traduis 曼城 », max_tokens=16 puis 80 :
        #   qwen/qwen3.8-27b     → « Manchester City » aux DEUX plafonds ✅
        #   qwen/qwen3.6-27b     → crache un bloc <think>, finish=length ❌
        #   openai/gpt-oss-20b   → contenu VIDE aux deux plafonds       ❌
        #   openai/gpt-oss-120b  → vide à 16, correct seulement dès ~200 ❌
        # Les gpt-oss restent en repli : ils sont sains, mais seulement pour
        # les appels qui laissent de la marge (settlement à 2048).
        models=("qwen/qwen3.8-27b", "openai/gpt-oss-120b", "openai/gpt-oss-20b"),
        lanes=(FILTER, ANALYZE, SETTLEMENT, SEARCH_READ),
        # 160 et non 400 : la vraie contrainte de Groq n'est pas un nombre de
        # requetes mais 100 000 TOKENS PAR JOUR, comptes PAR ORGANISATION (une
        # 2e cle du meme compte ne rachete rien). Les appels de ce pipeline
        # tournent autour de 600 tokens (lots Pinnacle de 25 matchs, contextes
        # Tavily), soit ~165 appels avant epuisement. Un budget de 400 laissait
        # croire a une reserve qui n'existe pas : on aurait continue d'appeler
        # apres l'epuisement du TPD, et surtout on aurait epuise le TPD sur des
        # completions simples alors que compound-mini en a besoin.
        rpm=30, daily_requests=160, daily_tokens=100_000,
        note="TPD 100k compte PAR ORGANISATION, pas par cle. SEUL fournisseur du "
             "registre a porter groq/compound-mini (recherche web integree) : son "
             "quota est donc irremplacable, d'ou un budget serre et la reserve "
             "settlement qui ampute les autres lanes.",
    ),
    # ── Palier gratuit permanent, sans carte — priorité 1 ──
    # ⚠️ CERBRAS ET SAMBANOVA : `payment_required`, TRANCHÉ PAR L'INFÉRENCE.
    # Leur catalogue répond 200 avec une vraie clé, mais le premier appel
    # d'inférence rend 402 :
    #   Cerebras  → {"code":"payment_required"} sur ses 2 modèles
    #   SambaNova → {"code":"PAYMENT_METHOD_REQUIRED","balance_units":0}
    # C'est la leçon qui complète celle du 403 : un catalogue lisible ne prouve
    # PAS qu'un fournisseur est utilisable. Seul un appel d'inférence tranche.
    # Marqués `payment_required` → exclus de la production, gardés au registre
    # pour que personne ne repaie l'enquête.
    Provider(
        name="cerebras", base_url="https://api.cerebras.ai/v1",
        env_key="CEREBRAS_API_KEY",
        models=("gpt-oss-120b", "gemma-4-31b"),
        lanes=(FILTER, ANALYZE, SETTLEMENT, SEARCH_READ),
        rpm=30, daily_requests=250, terms_flag="payment_required",
        note="cle valide, catalogue 200 (2 modeles), mais inference 402 "
             "payment_required — verifie le 2026-08-22",
    ),
    Provider(
        name="gemini",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        env_key="GEMINI_API_KEY",
        # Ordre établi par INFÉRENCE RÉELLE le 2026-08-22. `gemini-2.5-flash`
        # est un modèle à RÉFLEXION : interrogé avec max_tokens=10 il rend un
        # contenu VIDE (`finish_reason=length`, `completion_tokens=0`) — tout
        # le budget est parti en réflexion avant le premier mot. Mesuré sur
        # « say OK » : flash = 41 tokens facturés pour 1 token utile,
        # flash-lite = 4. Sous les plafonds de ce pipeline (80 tokens pour un
        # alias), flash ne rendrait jamais rien. Le lite passe donc devant.
        models=("gemini-2.5-flash-lite", "gemini-2.5-flash"),
        lanes=(ANALYZE, FILTER, TRANSLATE_CJK),
        rpm=15, daily_requests=200,
        note="⚠️ NE PAS CONFONDRE avec le grounding Google Search, lui bien MORT en "
             "gratuit (limit:0, verifie sur 4 cles le 2026-07-21 — c'est pourquoi "
             "core/oracle.py est passe a Groq/Tavily). La GENERATION simple garde un "
             "palier gratuit. Ce fournisseur ne sert donc JAMAIS la lane SEARCH_READ.",
    ),
    Provider(
        name="cloudflare",
        base_url="https://api.cloudflare.com/client/v4/accounts/${CLOUDFLARE_ACCOUNT_ID}/ai",
        env_key="CLOUDFLARE_API_TOKEN",
        chat_path="/v1/chat/completions",
        catalog_path="/models/search?per_page=100",
        # Catalogue constaté le 2026-08-22 : 29 modèles texte, dont GLM-4.7-Flash
        # et GLM-5.2 — d'où la lane CJK, que ce fournisseur sert aussi bien que
        # Zhipu et sans clause non commerciale.
        models=("@cf/meta/llama-3.3-70b-instruct-fp8-fast",
                "@cf/zai-org/glm-4.7-flash",
                "@cf/openai/gpt-oss-120b",
                "@cf/deepseek-ai/deepseek-v4-flash-0731"),
        lanes=(FILTER, ANALYZE, SEARCH_READ, TRANSLATE_CJK, SETTLEMENT),
        rpm=0, daily_requests=200,
        note="10 000 neurons/j. Demande AUSSI CLOUDFLARE_ACCOUNT_ID : l'identifiant "
             "de compte est dans l'URL. Catalogue sous /ai/models/search, inference "
             "sous /ai/v1 — d'ou chat_path.",
    ),
    Provider(
        name="nebius", base_url="https://api.studio.nebius.ai/v1",
        env_key="NEBIUS_API_KEY",
        models=("Qwen/Qwen3-32B", "meta-llama/Llama-3.3-70B-Instruct"),
        lanes=(FILTER, ANALYZE, TRANSLATE_CJK, SEARCH_READ),
        rpm=0, daily_requests=150,
        note="credits gratuits a l'inscription ; catalogue derriere cle (401 sans cle). "
             "Nebius a renomme « AI Studio » en « Token Factory » : api.studio.nebius.ai "
             "et api.tokenfactory.nebius.com repondent tous deux 401 (donc vivants) — "
             "si l'un est retire un jour, basculer sur l'autre.",
    ),
    Provider(
        name="chutes", base_url="https://llm.chutes.ai/v1",
        env_key="CHUTES_API_KEY",
        # Noms EXACTS du catalogue (suffixe -TEE, constate le 2026-08-22) :
        # sans lui, l'API rend 404 « model not found ».
        models=("Qwen/Qwen3-32B-TEE",
                "deepseek-ai/DeepSeek-V4-Flash-0731-TEE",
                "zai-org/GLM-5.2-TEE"),
        lanes=(FILTER, ANALYZE, TRANSLATE_CJK, SEARCH_READ, SETTLEMENT),
        rpm=0, daily_requests=150, terms_flag="payment_required",
        note="catalogue PUBLIC lisible (14 modeles) mais inference 402 sur TOUS : "
             "« Quota exceeded and account balance is $0.0 ». Compte a crediter. "
             "Verifie le 2026-08-22.",
    ),
    Provider(
        name="openrouter", base_url="https://openrouter.ai/api/v1",
        env_key="OPENROUTER_API_KEY",
        # Préférences vérifiées présentes au catalogue :free du 2026-08-22.
        # Elles PÉRIMERONT — c'est le principe même du module.
        # Ordre établi par INFÉRENCE RÉELLE le 2026-08-22, pas par taille de
        # modèle. `gemma-4-31b-it` rend « OK » en 1 token ; le nemotron-super,
        # qui est un modèle de RAISONNEMENT, a dépensé 8 de ses 10 tokens en
        # réflexion et répondu « We are to say "OK" as per the… ». Sous les
        # plafonds serrés de ce pipeline (max_tokens=80 pour un alias, 300
        # pour l'oracle), un modèle de raisonnement brûle son budget à penser
        # et ne rend jamais le JSON attendu. Les instruct d'abord, donc.
        models=("nvidia/nemotron-3-nano-30b-a3b:free",
                "google/gemma-4-31b-it:free",
                "nvidia/nemotron-3-super-120b-a12b:free",
                "z-ai/glm-5.2:free",
                "nvidia/nemotron-nano-9b-v2:free",
                "liquid/lfm-2.5-2.6b:free"),
        lanes=(ANALYZE, FILTER, TRANSLATE_CJK, SEARCH_READ),
        # 40 et non 150 : l'endpoint /key confirme `is_free_tier: true` et
        # `total_credits: 0`. Le palier gratuit d'OpenRouter plafonne à ~50
        # requetes/jour (1000 apres un credit unique de 10 $). Un budget
        # PREDATOR SUPERIEUR au plafond du fournisseur ne sert a rien : il
        # garantit seulement qu'on continue d'appeler pour recolter des 429.
        # On veut basculer AVANT de se faire couper — regle heritee du compte
        # api-sports trouve SUSPENDU le 2026-08-20.
        rpm=20, daily_requests=40,
        headers={"HTTP-Referer": "https://github.com/predator-paim",
                 "X-Title": "PREDATOR"},
        note="421 modeles, 18 en :free (2026-08-22) ; ~50 req/j, 1000/j apres credit unique",
    ),
    Provider(
        name="nvidia_nim", base_url="https://integrate.api.nvidia.com/v1",
        env_key="NVIDIA_NIM_API_KEY",
        # Ordre par cout mesure le 2026-08-22 : deepseek-v4-flash rend « OK »
        # en 11 tokens, llama-3.3-70b en 42.
        models=("deepseek-ai/deepseek-v4-flash-0731",
                "meta/llama-3.3-70b-instruct"),
        lanes=(ANALYZE, FILTER, SEARCH_READ),
        rpm=40, daily_requests=200, terms_flag="evaluation",
        note="102 modeles, cle testee OK le 2026-08-22. terms_flag VERIFIE (et non "
             "plus suppose) : NVIDIA reserve le palier gratuit au developpement, "
             "test, recherche et evaluation ; « servir de vrais utilisateurs » ou "
             "« conduire des transactions » releve de la PRODUCTION et exige une "
             "licence AI Enterprise. PREDATOR mise de l'argent reel : cet usage "
             "est de la production. Exclu des lanes de production, sans exception.",
    ),
    Provider(
        name="sambanova", base_url="https://api.sambanova.ai/v1",
        env_key="SAMBANOVA_API_KEY",
        models=("Meta-Llama-3.3-70B-Instruct", "DeepSeek-V3.2", "gpt-oss-120b"),
        lanes=(FILTER, ANALYZE, SETTLEMENT, SEARCH_READ),
        rpm=60, daily_requests=200, terms_flag="payment_required",
        note="catalogue 200 (7 modeles) mais inference 402 PAYMENT_METHOD_REQUIRED, "
             "balance_units=0 — carte OBLIGATOIRE, verifie le 2026-08-22",
    ),
    Provider(
        name="ovh", base_url="https://oai.endpoints.kepler.ai.cloud.ovh.net/v1",
        env_key="OVH_AI_API_KEY",
        models=("Meta-Llama-3_3-70B-Instruct", "Qwen3-32B",
                "Mistral-Small-3.2-24B-Instruct-2506"),
        lanes=(FILTER, ANALYZE, TRANSLATE_CJK, SEARCH_READ),
        rpm=0, daily_requests=150,
        note="24 modeles (2026-08-22) ; souverainete UE",
    ),
    Provider(
        name="scaleway", base_url="https://api.scaleway.ai/v1",
        env_key="SCALEWAY_API_KEY",
        # Noms EXACTS du catalogue (15 modeles, 2026-08-22). `qwen3-32b`, ma
        # preference d'origine, n'existe pas chez Scaleway.
        models=("llama-3.3-70b-instruct", "gemma-4-26b-a4b-it",
                "glm-5.2", "mistral-small-3.2-24b-instruct-2506"),
        lanes=(FILTER, ANALYZE, SEARCH_READ, TRANSLATE_CJK),
        rpm=0, daily_requests=150, terms_flag="quota_zero",
        note="souverainete UE. Cle testee le 2026-08-22 : le catalogue repond 200 "
             "(15 modeles) mais TOUTE inference rend 429 « INSUFFICIENT QUOTA », y "
             "compris sur un appel isole apres 70 s d'attente — ce n'est donc pas "
             "une rafale mais un quota a ZERO. Les Generative APIs Scaleway "
             "n'accordent de quota qu'a un compte VALIDE. A retester une fois le "
             "compte valide : le catalogue, lui, est riche (glm-5.2 pour le CJK).",
    ),
    Provider(
        name="ollama_cloud", base_url="https://ollama.com/v1",
        env_key="OLLAMA_API_KEY",
        # Ordre etabli par inference reelle le 2026-08-22 AVEC CETTE CLE :
        # gpt-oss:120b repond « OK » ; glm-5.2 et deepseek-v4-flash rendent
        # 403 « this model requires a subscription ». Le palier gratuit ne
        # donne donc acces qu'a une PARTIE du catalogue — mettre un modele
        # payant en tete aurait ecarte le fournisseur a chaque run.
        models=("gpt-oss:120b", "gemma4:31b", "glm-5.2", "kimi-k3"),
        lanes=(SETTLEMENT, ANALYZE, TRANSLATE_CJK),
        rpm=0, daily_requests=100,
        catalog_path="/models",
        note="19 modeles au catalogue mais tous ne sont pas dans le palier gratuit "
             "(403 « requires a subscription »). 1 requete a la fois, sessions 5h "
             "— convient au batch de nuit, pas au scan.",
    ),
    Provider(
        name="cohere", base_url="https://api.cohere.com/compatibility/v1",
        env_key="COHERE_API_KEY",
        # Les preferences d'origine — command-r-plus et command-r — ont ete
        # SUPPRIMEES par Cohere le 15/09/2025 (404 explicite). Remplacees
        # d'apres le catalogue reel du 2026-08-22 (31 modeles).
        # command-a-translate est un modele SPECIALISE traduction, d'ou la
        # lane CJK.
        models=("command-a-03-2025", "command-a-plus-05-2026",
                "command-a-translate-08-2025", "command-r7b-12-2024"),
        lanes=(ANALYZE, TRANSLATE_CJK),
        rpm=0, daily_requests=30, terms_flag="non_commercial",
        note="cle testee OK le 2026-08-22 (command-a-03-2025, 6 tokens). "
             "1000 appels/mois ; cle d'essai NON COMMERCIALE — hors production.",
    ),
    # ── Asie — priorité 2, excellents en CJK (double emploi mission 3) ──
    Provider(
        name="zhipu", base_url="https://api.z.ai/api/paas/v4",
        env_key="ZHIPU_API_KEY",
        models=("glm-4.7-flash", "glm-4.5-flash"),
        lanes=(TRANSLATE_CJK, FILTER),
        rpm=0, daily_requests=200, terms_flag="non_commercial",
        note="GLM-Flash gratuit ; carve-out recherche/non-commercial. ⚠️ DEUX cles "
             "essayees le 2026-08-22, toutes deux refusees en 401 « token expired or "
             "incorrect » sur api.z.ai ET open.bigmodel.cn. Les cles Zhipu sont de la "
             "forme {id}.{secret} ; celles fournies etaient du hex 32 sans point, "
             "donc vraisemblablement une moitie seulement. Sans consequence : "
             "Cloudflare sert GLM-4.7-Flash et GLM-5.2 pour la lane CJK, sans clause "
             "non commerciale.",
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
        # Catalogue constaté le 2026-08-22 (10 modèles) : solar-pro4 est le
        # courant, solar-mini répond « OK » en 16 tokens.
        models=("solar-pro4", "solar-pro2", "solar-mini"),
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
    out = []
    for p in REGISTRY:
        if not p.key():
            continue
        if "${" in p.resolved_base:
            log.warning("ai_router[%s]: clé présente mais variable d'URL absente "
                        "(%s) — fournisseur ignoré", p.name, p.base_url)
            continue
        out.append(p)
    return out


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

    items = body.get("data") or body.get("models") or body.get("result") or []
    ids = set()
    for m in items:
        if isinstance(m, dict):
            # TOUS les champs qui peuvent porter une référence de modèle, pas
            # le premier : Cloudflare publie un `id` (UUID interne) ET un
            # `name` (`@cf/meta/llama-...`). Préférer `id` indexait 64 UUID et
            # concluait « aucune préférence au catalogue » sur un fournisseur
            # parfaitement sain — constaté le 2026-08-22.
            candidates = [m.get("id"), m.get("name"), m.get("model")]
        elif isinstance(m, str):
            candidates = [m]
        else:
            continue
        for mid in candidates:
            if not mid:
                continue
            mid = str(mid)
            ids.add(mid)
        # Certains catalogues préfixent leurs identifiants alors que
        # l'inférence accepte les deux formes : Gemini publie
        # `models/gemini-2.5-flash` et répond aussi bien à
        # `gemini-2.5-flash`. Sans cette normalisation, une préférence non
        # préfixée serait jugée ABSENTE et le fournisseur écarté à tort —
        # le routeur débrancherait un fournisseur parfaitement sain.
            if "/" in mid and not mid.startswith("@"):
                # `models/gemini-2.5-flash` → aussi `gemini-2.5-flash`.
                # Exclu pour les `@cf/...` de Cloudflare, dont le préfixe fait
                # partie intégrante de l'identifiant attendu à l'inférence.
                ids.add(mid.rsplit("/", 1)[-1])
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


def resolve_models(p: Provider, catalog: set | None = None) -> list:
    """TOUTES les préférences présentes au catalogue, dans l'ordre.

    POURQUOI PLUSIEURS — mesuré le 2026-08-22 sur OpenRouter : les modèles
    `:free` sont bridés EN AMONT, modèle par modèle et de façon fluctuante.
    Au même instant, `google/gemma-4-31b-it:free` rendait 429 « temporarily
    rate-limited upstream » pendant que `nvidia/nemotron-3-nano-30b-a3b:free`
    répondait « OK » proprement. Ne retenir qu'un seul modèle par fournisseur
    revenait donc à jeter tout OpenRouter parce qu'UN de ses modèles était
    saturé — alors que quatre autres étaient disponibles.
    """
    if not p.models:
        return []
    cat = fetch_catalog(p) if catalog is None else catalog
    if not cat:
        return list(p.models)          # catalogue muet : on garde les préférences
    return [m for m in p.models if m in cat]


# Codes sur lesquels il vaut la peine d'essayer un AUTRE modèle du MÊME
# fournisseur : la limite est portée par le modèle, pas par le compte.
# Codes qui disqualifient LE MODÈLE, pas le fournisseur — donc on essaie le
# suivant du même fournisseur :
#   404/410  « ce modèle n'existe plus » — Cloudflare a rendu 410 sur
#            @cf/meta/llama-3.1-8b-instruct, « deprecated on 2026-05-30 » ;
#   429/5xx  saturation amont, fluctuante modèle par modèle chez OpenRouter ;
#   403      Ollama Cloud rend 403 « this model requires a subscription » sur
#            glm-5.2 alors que gpt-oss:120b répond normalement AVEC LA MÊME
#            CLÉ. Un 403 n'est donc pas toujours « clé refusée ».
# Le risque d'inclure 403 — épuiser la liste sur une clé réellement invalide —
# est borné : chaque tentative compte comme un échec, et le disjoncteur s'ouvre
# au bout de BREAKER_THRESHOLD. On perd au pire deux ou trois appels, contre la
# perte d'un fournisseur entier dans l'autre sens.
_RETRY_NEXT_MODEL = (403, 404, 410, 429, 500, 502, 503, 504)

# Sentinelle : « ce modèle est saturé, essaie le suivant du MÊME fournisseur ».
# Distincte de None, qui veut dire « ce fournisseur est en panne, passe au suivant ».
_RETRY = object()


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


# Marge de tolerance sur la cadence : a 30 % de la journee ecoulee, un
# fournisseur peut avoir consomme jusqu'a 45 % de son budget sans etre
# deprioritise. Assez lache pour absorber une rafale de scans legitime, assez
# serre pour qu'un run pathologique ne vide pas la reserve avant midi.
PACING_HEADROOM = float(os.environ.get("AI_PACING_HEADROOM", "0.15"))


def _day_fraction(now: datetime | None = None) -> float:
    """Part de la journee UTC ecoulee, dans ]0,1]. Les compteurs de
    `daily_quota` sont indexes par date UTC : le cycle est donc bien 24 h,
    minuit a minuit."""
    n = now or _now()
    return max(0.02, (n.hour * 3600 + n.minute * 60 + n.second) / 86400)


def _pacing(p: Provider, lane: str, now: datetime | None = None) -> tuple:
    """(en avance sur la cadence ?, part de budget restante 0..1).

    « En avance » = ce fournisseur a deja consomme plus que sa part de la
    journee. On ne l'EXCLUT pas — l'exclure a 00 h 05 bloquerait tout — on le
    fait simplement passer apres les autres.
    """
    ceiling = p.daily_requests
    if not ceiling:
        return False, 1.0
    if lane != SETTLEMENT and SETTLEMENT in p.lanes:
        ceiling = max(1, ceiling - SETTLEMENT_RESERVE)
    spent = daily_quota.spent(p.bucket)
    autorise = ceiling * (_day_fraction(now) + PACING_HEADROOM)
    return spent > autorise, max(0.0, (ceiling - spent) / ceiling)


def lane_providers(lane: str, allow_flagged: bool = False,
                   balanced: bool = True) -> list:
    """Fournisseurs sains de la lane, dans l'ordre de préférence du registre.

    Écartés : clé absente, disjoncteur ouvert, budget épuisé pour cette lane,
    aucun modèle au catalogue, et — sauf `allow_flagged` — ceux dont les
    conditions réservent le gratuit à un usage non commercial/évaluation.

    ORDRE — c'est ici que se joue l'exploitation réelle de la capacité.
    Suivre l'ordre du registre draine TOUJOURS le premier fournisseur : sa
    réserve part en quelques heures pendant que les autres restent intacts,
    et le soir il ne reste plus rien alors que l'essentiel du budget du jour
    n'a jamais été touché. `balanced=True` trie donc par budget RESTANT — le
    moins servi d'abord — en reléguant ceux qui sont en avance sur la cadence
    du jour. L'ordre du registre ne sert plus que de départage : il reste la
    préférence de qualité quand tout le monde est à égalité.
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
        models = resolve_models(p)
        if not models:
            continue
        out.append((p, models))
    if balanced and len(out) > 1:
        rang = {q.name: i for i, q in enumerate(REGISTRY)}

        def cle(item):
            prov, _ = item
            en_avance, restant = _pacing(prov, lane)
            return (en_avance, -restant, rang[prov.name])

        out.sort(key=cle)
        log.debug("lane %s : ordre equilibre %s", lane, [q.name for q, _ in out])
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
        log.warning("%s[%s/%s]: HTTP %d: %s", label, p.name, model,
                    r.status_code, r.text[:180])
        return _RETRY if r.status_code in _RETRY_NEXT_MODEL else None
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

    for p, models in candidates:
        text = None
        for model in models:
            text = call_provider(p, model, messages, max_tokens, temperature,
                                 timeout, label)
            if text is _RETRY:
                continue          # modèle saturé — un autre du même fournisseur
            break
        if text is _RETRY or text is None:
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


# Prompt de vérification : court, sans ambiguïté, et dont la réponse attendue
# tient en un token. Il sert à distinguer les trois états qu'un catalogue seul
# ne distingue pas — clé refusée, quota/paiement requis, fournisseur utilisable.
_VERIFY_PROMPT = "Reply with exactly: OK"


def verify(timeout: int = 75) -> list:
    """Teste CHAQUE fournisseur configuré par une INFÉRENCE RÉELLE.

    POURQUOI PAS SEULEMENT LE CATALOGUE — c'est la leçon du 2026-08-22.
    Cerebras et SambaNova rendent tous deux un catalogue en HTTP 200 avec une
    clé valide, et refusent la première inférence en 402 :

        Cerebras  → {"code":"payment_required"}
        SambaNova → {"code":"PAYMENT_METHOD_REQUIRED","balance_units":0}

    Un catalogue lisible ne prouve donc RIEN sur l'utilisabilité. Seul un
    appel d'inférence tranche, et c'est ce que fait cette fonction.

    Rend une liste de dicts (fournisseur, modèle retenu, état, détail). Ne lève
    jamais : c'est un diagnostic, il doit survivre à tout.
    """
    out = []
    for p in REGISTRY:
        if not p.key():
            out.append({"provider": p.name, "state": "absent",
                        "detail": f"{p.env_key} non configurée"})
            continue
        if "${" in p.resolved_base:
            manquantes = re.findall(r"\$\{([A-Z0-9_]+)\}", p.resolved_base)
            out.append({"provider": p.name, "state": "incomplet",
                        "detail": f"variable(s) d'URL absente(s): {', '.join(manquantes)}"})
            continue

        catalog = fetch_catalog(p)
        model, switched = resolve_model(p, catalog)
        if not model:
            out.append({"provider": p.name, "state": "sans-modele",
                        "detail": f"aucune préférence au catalogue ({len(catalog)} modèles)"})
            continue

        try:
            r = requests.post(
                p.chat_url,
                json={"model": model,
                      "messages": [{"role": "user", "content": _VERIFY_PROMPT}],
                      "max_tokens": 200, "temperature": 0.0},
                headers={"Authorization": f"Bearer {p.key()}",
                         "Content-Type": "application/json", **p.headers},
                timeout=timeout)
        except Exception as e:
            out.append({"provider": p.name, "model": model, "state": "injoignable",
                        "detail": str(e)[:120]})
            continue

        if r.status_code != 200:
            etat = {401: "cle-refusee", 403: "cle-refusee",
                    402: "paiement-requis", 429: "quota"}.get(r.status_code, "erreur")
            out.append({"provider": p.name, "model": model, "state": etat,
                        "detail": f"HTTP {r.status_code} {r.text[:110]}"})
            continue

        try:
            text, tokens = _extract(r.json())
        except Exception as e:
            out.append({"provider": p.name, "model": model, "state": "illisible",
                        "detail": str(e)[:110]})
            continue
        if not text or not text.strip():
            # Cas Gemini 2.5 Flash : réponse vide parce que tout le budget est
            # parti en réflexion. Le fournisseur répond, mais il est inutile
            # sous les plafonds serrés de ce pipeline.
            out.append({"provider": p.name, "model": model, "state": "reponse-vide",
                        "detail": f"{tokens} tokens facturés, 0 utile"})
            continue
        out.append({"provider": p.name, "model": model, "state": "OK",
                    "switched": switched, "tokens": tokens,
                    "detail": text.strip()[:40]})
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
