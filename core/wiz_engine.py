"""
core/wiz_engine.py — WIZ (PAIM v10.0) : prompts, parsing, pondération, scoring.

MISSION (par ordre de rentabilité décroissante) :

  1. DÉTECTER LE FAUX EDGE. Un edge élevé a deux causes possibles. Soit le
     soft book est lent à ajuster (vrai edge → on mise), soit le soft book
     SAIT quelque chose que le modèle ignore : titulaire absent, gardien
     remplaçant, lanceur MLB changé, équipe déjà qualifiée, joueur au repos.
     Dans le second cas la cote est haute POUR UNE RAISON, et l'edge est un
     piège. Tout le reste de ce module est subordonné à cette fonction.
  2. CLASSER les signaux par probabilité de réussite.

CE QUE WIZ NE FAIT PAS. Il ne modifie ni ne recalcule edge_pct, sharp_prob,
pinnacle_price, xbet_odd, kelly_pct, risk_flag. Il n'écrit aucune colonne de
`signals`. L'edge de PREDATOR est quantitatif et validé (asymétrie
Pinnacle/soft book) ; les données qualitatives collectées ici sont
statistiquement perdantes en moyenne dès qu'on les laisse toucher au calcul.
Wiz module un CLASSEMENT, il ne décide pas — d'où W_EDGE > W_WIZ dans le
score composite (core/constants.py).

TROIS GARDE-FOUS MÉCANIQUES, pas déclaratifs :

  - R3, consensus contrarian : le Tier C a un poids NÉGATIF dans
    WIZ_TIER_WEIGHTS. Un consensus public massif dans le sens du signal
    dégrade le score au lieu de le confirmer. Ce n'est pas une consigne
    dans le prompt (qu'un modèle peut ignorer), c'est le signe d'un
    coefficient.
  - R4, jamais de donnée inventée : _validate() jette tout argument dont
    l'URL source n'apparaît pas dans les résultats de recherche réellement
    obtenus. Un modèle qui hallucine une source voit son argument
    disparaître, quoi qu'il affirme.
  - Verdict borné : le verdict du LLM n'est retenu que s'il n'est pas plus
    optimiste que ce que ses PROPRES arguments pondérés justifient. On
    corrige vers le bas, jamais vers le haut.

En cas d'échec (recherche impossible, IA morte, JSON illisible) le verdict
est INDISPONIBLE — jamais un score fabriqué, jamais une exception qui casse
le run. « Je n'ai pas pu chercher » n'est pas « l'information n'existe pas ».
"""
import json
import logging
import re
from datetime import datetime, timezone

from core.constants import (
    WIZ_ALERTE_SCORE,
    WIZ_CONFIDENCE_CEILING,
    WIZ_CONFIRME_SCORE,
    WIZ_EDGE_NORM_CAP,
    WIZ_HIGH_SEVERITY_FORCES_ALERTE,
    WIZ_MAX_ARGUMENTS,
    WIZ_MAX_RED_FLAGS,
    WIZ_NEUTRAL_CONFIDENCE,
    WIZ_NEUTRAL_CONSENSUS,
    WIZ_QUERIES_PER_MATCH,
    WIZ_SEVERITY_WEIGHTS,
    WIZ_TIER_WEIGHTS,
    WIZ_VETO_SCORE,
    WIZ_W_CONS,
    WIZ_W_EDGE,
    WIZ_W_WIZ,
)

log = logging.getLogger("PREDATOR.wiz_engine")

# Du meilleur au pire. L'ordre EST la sémantique : _worst() s'en sert pour
# borner le verdict du LLM, et /wiz pour colorer les badges.
VERDICTS = ("CONFIRME", "NEUTRE", "ALERTE", "VETO")
INDISPONIBLE = "INDISPONIBLE"

_VALID_TIERS      = tuple(WIZ_TIER_WEIGHTS)
_VALID_DIRECTIONS = ("pour", "contre")
_VALID_SEVERITIES = tuple(WIZ_SEVERITY_WEIGHTS)


# ══════════════════════════════════════════════════════════════════════
# 1. Construction des requêtes de recherche
# ══════════════════════════════════════════════════════════════════════
#
# 2 requêtes par match maximum, ciblées Tier A exclusivement. Ce n'est pas
# une économie de confort : Brave est le goulot d'étranglement (~2 000
# requêtes/mois), et le Tier A est le seul qui puisse EXPLIQUER un edge.
# Chercher la forme récente (Tier B) coûterait le même crédit pour une
# information qui, elle, est déjà dans la cote.

# Requête 1 par sport — ce qui, dans ce sport, change une ligne du jour au
# lendemain. Le vocabulaire est en anglais parce que c'est la langue des
# sources qui publient ces informations en premier (beat writers, comptes
# officiels), pas par préférence.
_SPORT_QUERY_A = {
    "soccer":      "team news lineup injuries suspensions",
    "basketball":  "injury report starting lineup back-to-back rest",
    "euroleague_basketball": "injury report starting lineup back-to-back rest",
    "americanfootball": "injury report inactives starting quarterback weather",
    "baseball":    "starting pitcher confirmed lineup",
    "hockey":      "starting goalie confirmed lineup injuries",
    "rugbyleague": "team list injuries late changes",
    "aussierules": "team news selection injuries late out",
}
_DEFAULT_QUERY_A = "team news lineup injuries"

# Requête 2 — l'ENJEU, angle mort classique du modèle quantitatif : une
# équipe déjà qualifiée fait tourner, un match sans enjeu se joue à 70%.
# La cote intègre ça bien avant le devigging.
_QUERY_B_STAKE = "preview stakes already qualified dead rubber rotation"

# Sur les totals, la météo déplace la ligne plus que n'importe quelle
# absence individuelle — elle remplace donc l'angle « enjeu ».
_QUERY_B_TOTALS = "weather forecast wind conditions preview"


def build_queries(match: str, sport: str, market_keys=(), kickoff: str = "") -> list[str]:
    """2 requêtes Brave max pour un match, adaptées au sport et aux marchés.

    `market_keys` est l'ensemble des marchés couverts par les signaux de ce
    match (h2h, totals, spreads) — un seul jeu de requêtes les sert tous,
    c'est tout l'intérêt du cache par match_id.
    """
    if not match:
        return []

    day = ""
    if kickoff:
        try:
            day = datetime.fromisoformat(kickoff.replace("Z", "+00:00")).strftime("%Y-%m-%d")
        except (ValueError, TypeError, AttributeError):
            day = ""

    topic_a = _SPORT_QUERY_A.get(sport, _DEFAULT_QUERY_A)
    has_totals = any("totals" in (k or "") for k in market_keys)
    topic_b = _QUERY_B_TOTALS if has_totals else _QUERY_B_STAKE

    suffix = f" {day}" if day else ""
    queries = [f"{match} {topic_a}{suffix}", f"{match} {topic_b}{suffix}"]
    return queries[:WIZ_QUERIES_PER_MATCH]


# ══════════════════════════════════════════════════════════════════════
# 2. Prompt
# ══════════════════════════════════════════════════════════════════════

_PROMPT = """Tu es un analyste de paris sportifs. On te donne un signal de value betting \
détecté quantitativement (écart entre la cote d'un bookmaker sharp et celle d'un \
bookmaker grand public), et des résultats de recherche web bruts.

TA MISSION PRINCIPALE n'est PAS de confirmer le signal. C'est de chercher ce qui \
pourrait EXPLIQUER cet écart de cote. Un écart élevé a deux causes possibles :
  (a) le bookmaker grand public est simplement lent à ajuster — le signal est bon ;
  (b) le bookmaker grand public SAIT quelque chose : titulaire absent, gardien \
remplaçant, lanceur changé, joueur au repos, équipe déjà qualifiée, match sans enjeu, \
météo extrême. Dans ce cas la cote est haute pour une raison, et le signal est un piège.
Cherche activement (b). C'est ce qu'on te demande.

SIGNAL
  Match       : {match}
  Sport       : {sport}{league_line}
  Coup d'envoi: {kickoff}
  Marchés     : {markets}
  Edge mesuré : +{edge:.2f}%

UTILISE TA RECHERCHE WEB pour couvrir ces axes :
{angles}

⚠️ Fais AU MAXIMUM {max_searches} recherches web AU TOTAL, puis réponds immédiatement. \
N'ouvre pas plus de quelques pages : au-delà tu satures ton contexte et tu ne peux \
plus répondre du tout.

RÈGLES ABSOLUES
1. N'affirme RIEN qui ne soit pas explicitement présent dans les pages que tu as \
réellement consultées. Pas de connaissance générale, pas de souvenir, pas de déduction \
sur l'actualité.
2. Chaque argument et chaque red flag DOIT citer dans "source_url" l'URL exacte d'une \
page que tu as consultée. Un argument dont l'URL ne correspond à aucune source \
réellement ouverte sera automatiquement rejeté.
3. Si la recherche ne donne rien d'exploitable sur ce match, réponds \
verdict "INDISPONIBLE" avec des listes vides. C'est une réponse correcte et attendue — \
ne comble pas le vide.
4. Le consensus des pronostiqueurs et les pourcentages de paris publics sont du \
Tier C : ce sont des indicateurs CONTRARIAN, jamais une confirmation. Classe-les en \
tier "C" et n'en tire aucune conclusion favorable.
5. Ne mets JAMAIS un élément de consensus ou de paris publics dans "red_flags". \
Un consensus est un drapeau JAUNE, pas une explication de l'écart de cote : il va \
dans "arguments" avec tier "C". "red_flags" est réservé aux faits de terrain qui \
expliquent la cote (absence, remplaçant, repos, enjeu, météo).

CLASSEMENT DES SOURCES
  tier "A" — décisif : absences/blessures confirmées, compositions probables, lanceur \
confirmé, back-to-back, météo (pour les totals), enjeu sportif du match.
  tier "B" — modérateur : forme récente, confrontations directes, stats, actualité du club.
  tier "C" — contrarian : consensus pronostiqueurs, % de paris publics.

"direction" vaut "pour" si l'élément soutient le signal, "contre" s'il le contredit.
"poids" est ta confiance dans CET élément, de 0.0 à 1.0.
"wiz_confidence" est la PROBABILITÉ, de 0 à 100, que ce pari soit GAGNANT. \
Ce n'est PAS ta confiance dans la qualité de ton analyse. Si tu rends un verdict \
VETO ou ALERTE, cette valeur doit être BASSE (le pari est mauvais), même si tu es \
très sûr de ton diagnostic.

Réponds UNIQUEMENT avec cet objet JSON, sans préambule, sans commentaire, sans \
balises markdown :
{{"verdict":"CONFIRME|NEUTRE|ALERTE|VETO|INDISPONIBLE","wiz_confidence":0,\
"resume":"2 phrases maximum","arguments":[{{"texte":"","source_url":"","tier":"A",\
"direction":"pour","poids":0.5}}],"red_flags":[{{"texte":"","source_url":"",\
"severite":"haute|moyenne|basse"}}]}}"""


_PROMPT_GROUNDED_HEAD = """\
⚠️ Tu n'as PAS d'outil de recherche. Les pages ci-dessous sont les SEULES \
que tu as consultées : tout ce que tu affirmes doit en sortir, et chaque \
"source_url" doit être copiée à l'identique depuis l'une d'elles.

RÉSULTATS DE RECHERCHE
{results}
"""


def build_prompt_from_results(ctx: dict, results_block: str) -> str:
    """Variante du prompt pour la cascade de core/wiz_sources.py : les pages
    sont fournies d'avance au lieu d'être cherchées par le modèle.

    Le contrat de sortie est identique au chemin nominal (mêmes règles, même
    schéma JSON) — `validate()` applique derrière exactement le même garde-fou
    R4, quel que soit le fournisseur qui a répondu. Seule change la phrase qui
    demandait une recherche web : promettre un outil que le modèle n'a pas
    l'amène à inventer des URLs plausibles, ce que R4 rejetterait en bloc.
    """
    base = build_prompt(ctx)
    base = base.replace(
        "UTILISE TA RECHERCHE WEB pour couvrir ces axes :",
        "AXES À COUVRIR dans les résultats fournis :")
    base = re.sub(r"⚠️ Fais AU MAXIMUM.*?répondre du tout\.\s*", "", base, flags=re.S)
    return _PROMPT_GROUNDED_HEAD.format(results=results_block) + "\n" + base


def build_prompt(ctx: dict) -> str:
    """Prompt complet pour un match. `ctx` est le contexte agrégé du match
    (voir analyze_match).

    Depuis l'abandon de Brave (2026-07-23), la recherche n'est plus faite en
    amont puis injectée : c'est le connecteur `web_search` de Mistral qui la
    fait pendant l'appel. On ne lui donne donc plus des résultats, mais les
    AXES à couvrir — ceux du Tier A, les seuls qui puissent expliquer un edge.
    Le garde-fou R4 n'est pas affaibli pour autant : les URLs réellement
    consultées reviennent dans la réponse (blocs `tool_reference`) et
    validate() y confronte chaque argument.
    """
    league = (ctx.get("league") or "").strip()
    markets = ctx.get("markets") or []
    angles = build_queries(ctx.get("match", ""), ctx.get("sport", ""),
                           ctx.get("market_keys") or [], ctx.get("kickoff") or "")
    return _PROMPT.format(
        match=ctx.get("match", "?"),
        sport=ctx.get("sport", "?"),
        league_line=f"\n  Ligue       : {league}" if league else "",
        kickoff=ctx.get("kickoff") or "inconnu",
        markets="; ".join(markets) if markets else "?",
        edge=float(ctx.get("edge_pct") or 0.0),
        angles="\n".join(f"  - {a}" for a in angles) or "  - actualité d'avant-match",
        max_searches=WIZ_QUERIES_PER_MATCH,
    )


# ══════════════════════════════════════════════════════════════════════
# 3. Parsing défensif
# ══════════════════════════════════════════════════════════════════════

def extract_json(text: str) -> dict | None:
    """Extrait l'objet JSON d'une réponse LLM, quoi qu'il y ait autour.

    Les modèles ajoutent régulièrement un préambule, des backticks markdown,
    ou une phrase de conclusion malgré la consigne. Trois passes, de la plus
    stricte à la plus permissive. Retourne None si rien n'est exploitable —
    jamais d'exception : un JSON illisible donne INDISPONIBLE, pas un run
    cassé.
    """
    if not text or not text.strip():
        return None
    raw = text.strip()

    # Passe 1 — c'est déjà du JSON propre.
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else None
    except (json.JSONDecodeError, ValueError):
        pass

    # Passe 2 — bloc ```json ... ``` (ou ``` ... ```).
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", raw, re.DOTALL | re.IGNORECASE)
    if fence:
        try:
            obj = json.loads(fence.group(1))
            if isinstance(obj, dict):
                return obj
        except (json.JSONDecodeError, ValueError):
            pass

    # Passe 3 — première accolade équilibrée du texte. Compte les niveaux en
    # ignorant les accolades à l'intérieur des chaînes (un `resume` contenant
    # une accolade casserait un simple find/rfind).
    start = raw.find("{")
    if start == -1:
        return None
    depth, in_str, escaped = 0, False, False
    for i in range(start, len(raw)):
        ch = raw[i]
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(raw[start:i + 1])
                    return obj if isinstance(obj, dict) else None
                except (json.JSONDecodeError, ValueError):
                    return None
    return None


def _clamp(value, lo: float, hi: float, default: float) -> float:
    try:
        return max(lo, min(hi, float(value)))
    except (TypeError, ValueError):
        return default


def _known_urls(results: list[dict]) -> set:
    return {(r.get("url") or "").strip() for r in (results or []) if (r.get("url") or "").strip()}


def validate(parsed: dict, results: list[dict]) -> dict:
    """Nettoie la sortie du LLM et applique R4 mécaniquement.

    Tout argument ou red flag dont source_url n'est pas EXACTEMENT l'une des
    URLs réellement retournées par la recherche est jeté. C'est ce qui rend
    « ne jamais inventer une donnée absente » vérifiable plutôt que
    déclaratif : un modèle peut ignorer une consigne de prompt, il ne peut
    pas faire apparaître une URL dans un set qu'il ne contrôle pas.

    Les champs invalides (tier inconnu, poids hors bornes) sont corrigés vers
    la valeur la plus neutre plutôt que de faire tomber l'analyse entière.
    """
    urls = _known_urls(results)

    args_out = []
    for a in (parsed.get("arguments") or [])[:WIZ_MAX_ARGUMENTS * 3]:
        if not isinstance(a, dict):
            continue
        texte = (a.get("texte") or "").strip()
        url   = (a.get("source_url") or "").strip()
        if not texte:
            continue
        if url not in urls:
            log.info("WIZ: argument rejeté (source non issue de la recherche): %r / %r",
                     texte[:60], url[:80])
            continue
        tier = (a.get("tier") or "").strip().upper()
        if tier not in _VALID_TIERS:
            tier = "B"   # défaut modérateur : ni décisif, ni contrarian
        direction = (a.get("direction") or "").strip().lower()
        if direction not in _VALID_DIRECTIONS:
            direction = "pour"
        args_out.append({
            "texte":      texte[:400],
            "source_url": url,
            "tier":       tier,
            "direction":  direction,
            "poids":      round(_clamp(a.get("poids"), 0.0, 1.0, 0.5), 3),
        })
        if len(args_out) >= WIZ_MAX_ARGUMENTS:
            break

    flags_out = []
    for f in (parsed.get("red_flags") or [])[:WIZ_MAX_RED_FLAGS * 3]:
        if not isinstance(f, dict):
            continue
        texte = (f.get("texte") or "").strip()
        url   = (f.get("source_url") or "").strip()
        if not texte:
            continue
        if url not in urls:
            log.info("WIZ: red flag rejeté (source non issue de la recherche): %r / %r",
                     texte[:60], url[:80])
            continue
        sev = (f.get("severite") or "").strip().lower()
        if sev not in _VALID_SEVERITIES:
            sev = "moyenne"
        flags_out.append({"texte": texte[:400], "source_url": url, "severite": sev})
        if len(flags_out) >= WIZ_MAX_RED_FLAGS:
            break

    verdict = (parsed.get("verdict") or "").strip().upper()
    if verdict not in VERDICTS and verdict != INDISPONIBLE:
        verdict = INDISPONIBLE

    resume = (parsed.get("resume") or "").strip()[:600]

    return {
        "verdict":        verdict,
        "wiz_confidence": round(_clamp(parsed.get("wiz_confidence"), 0.0, 100.0,
                                       WIZ_NEUTRAL_CONFIDENCE), 2),
        "resume":         resume,
        "arguments":      args_out,
        "red_flags":      flags_out,
    }


# ══════════════════════════════════════════════════════════════════════
# 4. Pondération et verdict
# ══════════════════════════════════════════════════════════════════════

def weighted_score(arguments: list, red_flags: list) -> float:
    """Score pondéré signé des éléments collectés. > 0 = rien n'explique
    l'edge ; < 0 = quelque chose l'explique (donc c'est un piège).

    Tiers A et B — contribution = poids × TIER × SENS, où TIER vient de
    WIZ_TIER_WEIGHTS (A=1.0, B=0.5) et SENS vaut +1 pour "pour", -1 pour
    "contre". Un fait de terrain qui contredit le signal fait baisser le
    score, proportionnellement à son caractère décisif.

    Tier C — contribution TOUJOURS NÉGATIVE, quelle que soit `direction` :
    -|poids × WIZ_TIER_WEIGHTS["C"]|. C'est R3 dans sa forme littérale :
    « ne traite jamais le consensus public comme prédictif », donc un
    élément Tier C ne peut JAMAIS améliorer un score. Deux raisons, dont
    la seconde n'est apparue qu'en conditions réelles :

      1. Doctrine. Faire remonter un signal parce que le public est à
         l'opposé, c'est encore traiter le consensus comme prédictif —
         juste dans l'autre sens. Un drapeau jaune reste jaune.
      2. Le champ `direction` est ambigu pour un LLM (constaté live le
         2026-07-23 sur mistral-small-latest). Le modèle écrit
         tier=C/direction="contre" pour signifier « ce consensus est un
         mauvais signe » — il exprime son JUGEMENT, là où l'encodage
         attendait le FAIT (« le public est-il dans notre sens ? »). Avec
         un poids de tier négatif, cette lecture produisait une double
         négation : 0.3 × (-0.35) × (-1) = +0.105, soit un risque signalé
         par le modèle qui AMÉLIORAIT le score. Aucune reformulation de
         prompt ne fiabilise ça — on retire simplement `direction` de
         l'équation pour le Tier C.

    Les red flags comptent toujours négativement, par sévérité.
    """
    score = 0.0
    for a in arguments or []:
        tier = a.get("tier")
        poids = _clamp(a.get("poids"), 0.0, 1.0, 0.5)
        if tier == "C":
            score -= abs(poids * WIZ_TIER_WEIGHTS["C"])
            continue
        tier_w = WIZ_TIER_WEIGHTS.get(tier, WIZ_TIER_WEIGHTS["B"])
        sense  = 1.0 if a.get("direction") == "pour" else -1.0
        score += poids * tier_w * sense
    for f in red_flags or []:
        score -= WIZ_SEVERITY_WEIGHTS.get(f.get("severite"), WIZ_SEVERITY_WEIGHTS["moyenne"])
    return round(score, 4)


def _verdict_from_score(score: float) -> str:
    if score <= WIZ_VETO_SCORE:
        return "VETO"
    if score <= WIZ_ALERTE_SCORE:
        return "ALERTE"
    if score >= WIZ_CONFIRME_SCORE:
        return "CONFIRME"
    return "NEUTRE"


def _worst(a: str, b: str) -> str:
    """Le plus pessimiste des deux verdicts (VERDICTS est ordonné)."""
    return a if VERDICTS.index(a) >= VERDICTS.index(b) else b


def decide_verdict(llm_verdict: str, arguments: list, red_flags: list) -> tuple[str, float]:
    """Verdict final = le LLM, borné vers le bas par ses propres arguments.

    Retourne (verdict, score_pondéré).

    Asymétrie volontaire : on corrige un verdict trop optimiste, jamais un
    verdict trop prudent. Un modèle qui annonce CONFIRME alors que ses
    arguments pointent contre est ramené à ALERTE ; un modèle qui annonce
    ALERTE avec des arguments favorables reste à ALERTE. Pour un détecteur
    de piège, un faux positif coûte un pari manqué, un faux négatif coûte
    une mise — l'asymétrie du coût justifie celle de la règle.

    INDISPONIBLE est absorbant : si le modèle dit qu'il n'a rien pu établir,
    aucun score d'arguments ne le contredit.
    """
    if llm_verdict == INDISPONIBLE:
        return INDISPONIBLE, 0.0
    if llm_verdict not in VERDICTS:
        return INDISPONIBLE, 0.0

    score = weighted_score(arguments, red_flags)
    verdict = _worst(llm_verdict, _verdict_from_score(score))

    # Un seul red flag de sévérité haute suffit à sortir de CONFIRME/NEUTRE,
    # quel que soit le reste : c'est exactement le cas que Wiz existe pour
    # attraper, on ne le laisse pas se diluer dans une moyenne d'arguments
    # favorables.
    if WIZ_HIGH_SEVERITY_FORCES_ALERTE and any(
            (f.get("severite") == "haute") for f in (red_flags or [])):
        verdict = _worst(verdict, "ALERTE")

    return verdict, score


def cap_confidence(verdict: str, confidence) -> float | None:
    """Borne wiz_confidence par le verdict (WIZ_CONFIDENCE_CEILING).

    Vérifié live le 2026-07-23 : mistral-small-latest renvoie volontiers
    verdict=VETO avec wiz_confidence=75, en lisant le champ comme « ma
    confiance dans cette analyse » plutôt que « ma confiance que le signal
    aboutisse ». Les deux lectures sont légitimes en français ; on ne peut
    pas les départager par une reformulation du prompt de façon fiable.

    Comme wiz_confidence pèse +0.35 dans rank_score, ne pas borner faisait
    remonter un match qualifié de piège EN TÊTE du classement. Le verdict,
    lui, est dérivé d'arguments dont chaque URL a été vérifiée — c'est la
    grandeur de confiance, donc c'est elle qui plafonne l'autre.
    """
    if verdict == INDISPONIBLE or confidence is None:
        return None
    ceiling = WIZ_CONFIDENCE_CEILING.get(verdict, 100.0)
    return round(min(_clamp(confidence, 0.0, 100.0, WIZ_NEUTRAL_CONFIDENCE), ceiling), 2)


# ══════════════════════════════════════════════════════════════════════
# 5. Score composite de classement
# ══════════════════════════════════════════════════════════════════════

def rank_score(edge_pct, wiz_confidence, consensus_score=None,
               verdict: str = INDISPONIBLE) -> float:
    """
    wiz_rank_score = W_EDGE*edge_norm + W_WIZ*(confiance/100) + W_CONS*(consensus/100)

    Le quantitatif garde la primauté (W_EDGE=0.45 est le poids le plus
    fort) : à confiance Wiz égale, l'ordre est celui de l'edge, et un
    signal à fort edge sans aucune information Wiz reste devant un signal à
    faible edge — c'est vérifié dans tests/test_wiz_engine.py.

    Un verdict INDISPONIBLE utilise WIZ_NEUTRAL_CONFIDENCE : l'absence
    d'information n'est pas une information négative. Un match qu'on n'a pas
    pu documenter ne doit être ni promu ni rétrogradé par rapport à ce que
    son edge seul lui vaut.

    `consensus_score` est l'accord entre sources sharp calculé par
    core/paim_engine.py, PAS un consensus de pronostiqueurs (celui-là est
    contrarian et vit dans le Tier C des arguments). Il est NULL sur les
    signaux à source unique (harvester/oracle) → valeur neutre.
    """
    edge_norm = _clamp(edge_pct, 0.0, WIZ_EDGE_NORM_CAP, 0.0) / WIZ_EDGE_NORM_CAP

    if verdict == INDISPONIBLE or wiz_confidence is None:
        conf = WIZ_NEUTRAL_CONFIDENCE
    else:
        conf = _clamp(wiz_confidence, 0.0, 100.0, WIZ_NEUTRAL_CONFIDENCE)

    cons = WIZ_NEUTRAL_CONSENSUS if consensus_score is None else \
        _clamp(consensus_score, 0.0, 100.0, WIZ_NEUTRAL_CONSENSUS)

    return round(
        WIZ_W_EDGE * edge_norm
        + WIZ_W_WIZ * (conf / 100.0)
        + WIZ_W_CONS * (cons / 100.0),
        4,
    )


# ══════════════════════════════════════════════════════════════════════
# 6. Orchestration d'un match
# ══════════════════════════════════════════════════════════════════════

def unavailable(ctx: dict, reason: str, queries_used: int = 0,
                sources_count: int = 0, model_used: str | None = None) -> dict:
    """Ligne wiz_analysis pour un match qu'on n'a pas pu documenter.

    Ce n'est PAS un échec silencieux : la ligne est écrite, avec son motif
    dans `resume`, pour qu'on puisse compter les INDISPONIBLE dans le temps.
    Un taux qui monte veut dire quota mort ou requêtes mal ciblées — sans
    trace en base, ça reste invisible.
    """
    return {
        "match_id":       ctx.get("match_id") or "",
        "match":          ctx.get("match") or "?",
        "sport":          ctx.get("sport"),
        "league":         ctx.get("league"),
        "signal_ids":     ctx.get("signal_ids") or [],
        "verdict":        INDISPONIBLE,
        "wiz_confidence": None,
        "wiz_rank_score": rank_score(ctx.get("edge_pct"), None,
                                     ctx.get("consensus_score"), INDISPONIBLE),
        "arguments":      [],
        "red_flags":      [],
        "resume":         reason,
        "sources_count":  sources_count,
        "model_used":     model_used,
        "queries_used":   queries_used,
    }


def analyze_match(ctx: dict, search_fn=None) -> dict:
    """Analyse un match et retourne une ligne prête à insérer dans wiz_analysis.

    `ctx` agrège TOUS les signaux actifs du match (le cache par match_id) :
        {match_id, match, sport, league, kickoff,
         markets:[...],      libellés lisibles, pour le prompt
         market_keys:[...],  clés brutes (h2h/totals/spreads), pour cibler
                             la 2e requête (météo sur les totals)
         signal_ids:[...], edge_pct: <le meilleur edge du match>,
         consensus_score}

    `search_fn(prompt) -> (texte, sources, modèle)` est injectable pour les
    tests ; en production c'est core.wiz_ai.mistral_search, qui fait la
    recherche web et le raisonnement en un seul appel (connecteur
    `web_search`). Les `sources` retournées sont les pages RÉELLEMENT
    consultées — c'est ce set qui sert de garde-fou R4 dans validate().

    Ne lève jamais. Tout chemin d'échec produit une ligne INDISPONIBLE.
    """
    if search_fn is None:
        # Cascade de sources (core/wiz_sources.py) : connecteur Mistral →
        # Google News (gratuit) → Tavily sous réserve, puis raisonnement en
        # chat pur. Avant cette cascade, une seule source morte suffisait à
        # rendre 85% des analyses INDISPONIBLE.
        from core import wiz_sources
        search_fn = wiz_sources.make_search_fn(ctx)

    if not (ctx.get("match") or "").strip():
        return unavailable(ctx, "Match sans nom — rien à chercher")

    # ── Recherche + raisonnement (un seul appel) ──────────────────────
    prompt = build_prompt(ctx)
    try:
        text, results, model = search_fn(prompt, label="WIZ")
    except Exception as e:   # un fournisseur qui lève ne doit pas tuer le run
        log.warning("WIZ: appel en échec sur %s: %s", ctx.get("match"), e)
        text, results, model = None, [], None

    used = 1 if text is not None or results else 0

    if not text:
        return unavailable(ctx, "IA indisponible (quota ou panne fournisseur)",
                           queries_used=used, sources_count=len(results or []))

    if not results:
        # R4 : distinguer les deux causes. Sans source réellement consultée,
        # tout ce que le modèle affirme serait de la connaissance interne —
        # exactement ce qu'on refuse d'écrire. Le motif est stocké tel quel,
        # il sert à diagnostiquer un taux d'INDISPONIBLE qui monte.
        return unavailable(ctx, "Aucune source web exploitable pour ce match",
                           queries_used=used, model_used=model)

    # Mode d'échec propre au connecteur web_search, constaté live le
    # 2026-07-23 : quand le modèle ouvre trop de pages, il rend
    # {"error": "Too many content was opened, result too big. Stop."} à la
    # place de l'analyse. Ce n'est pas un JSON illisible — c'est un JSON
    # parfaitement valide qui ne contient rien. On le nomme explicitement
    # plutôt que de le laisser se fondre dans « réponse non exploitable »,
    # parce que le remède est différent : baisser WIZ_QUERIES_PER_MATCH.
    if "Too many content was opened" in text:
        log.warning("WIZ: contexte saturé par la recherche sur %s (%d sources ouvertes) "
                    "— baisser WIZ_QUERIES_PER_MATCH si ça se répète",
                    ctx.get("match"), len(results))
        return unavailable(ctx, "Recherche trop large — contexte saturé",
                           queries_used=used, sources_count=len(results),
                           model_used=model)

    parsed = extract_json(text)
    if parsed is None:
        log.warning("WIZ: JSON illisible pour %s — réponse: %r", ctx.get("match"), text[:200])
        return unavailable(ctx, "Réponse IA non exploitable (JSON illisible)",
                           queries_used=used, sources_count=len(results),
                           model_used=model)

    clean = validate(parsed, results)

    # Un modèle qui rend un verdict tranché sans qu'aucun argument ne
    # survive à la validation d'URL n'a rien étayé — R4 s'applique au
    # verdict lui-même, pas seulement aux arguments pris un par un.
    if clean["verdict"] != INDISPONIBLE and not clean["arguments"] and not clean["red_flags"]:
        log.info("WIZ: verdict %s sans aucune source valide pour %s — ramené à INDISPONIBLE",
                 clean["verdict"], ctx.get("match"))
        row = unavailable(ctx, clean["resume"] or "Aucun argument sourcé",
                          queries_used=used, sources_count=len(results), model_used=model)
        return row

    verdict, score = decide_verdict(clean["verdict"], clean["arguments"], clean["red_flags"])
    confidence = cap_confidence(verdict, clean["wiz_confidence"])

    log.info("WIZ | %s | %s (score %.2f, confiance %s) | %d args, %d red flags, %d sources",
             ctx.get("match"), verdict, score,
             "-" if confidence is None else f"{confidence:.0f}",
             len(clean["arguments"]), len(clean["red_flags"]), len(results))

    return {
        "match_id":       ctx.get("match_id") or "",
        "match":          ctx.get("match") or "?",
        "sport":          ctx.get("sport"),
        "league":         ctx.get("league"),
        "signal_ids":     ctx.get("signal_ids") or [],
        "verdict":        verdict,
        "wiz_confidence": confidence,
        "wiz_rank_score": rank_score(ctx.get("edge_pct"), confidence,
                                     ctx.get("consensus_score"), verdict),
        "arguments":      clean["arguments"],
        "red_flags":      clean["red_flags"],
        "resume":         clean["resume"],
        "sources_count":  len(results),
        "model_used":     model,
        "queries_used":   used,
    }


def now_iso() -> str:
    """Horodatage UTC ISO — analyzed_at, et clé de l'unicité (match_id, analyzed_at)."""
    return datetime.now(timezone.utc).isoformat()
