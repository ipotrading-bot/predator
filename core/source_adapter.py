"""
core/source_adapter.py — cadre commun des sources de cotes.

CE QUE ÇA RÉSOUT
----------------
Chaque source branchée jusqu'ici (odds_api, odds_api_io, matchbook,
titan007) a réinventé son propre budget, son propre format de sortie et sa
propre idée de ce qu'est « le même match ». Tant qu'il y avait deux sources,
c'était tenable. À six, deux problèmes deviennent structurels :

  1. APPARIER SANS LES NOMS. odds.500.com écrit « 曼彻斯特联 », Polymarket écrit
     « Manchester United FC », Matchbook écrit « Man Utd ». Un appariement par
     nom entre ces trois-là échoue ou, pire, réussit sur la mauvaise équipe.
  2. SAVOIR À QUI FAIRE CONFIANCE. Deux chemins vers Pinnacle qui divergent,
     c'est un prix périmé quelque part — donc un edge imaginaire. Sans mesure
     de la divergence, cet edge part en signal.

Ce module tient les deux réponses, et rien d'autre : il ne parle à aucun
réseau et n'importe aucune source. Les adaptateurs l'importent, jamais
l'inverse — c'est ce qui permet de tester la logique d'appariement et de
divergence sans stub HTTP.

L'APPARIEMENT — STRUCTURE D'ABORD, NOM EN CONFIRMATION
-------------------------------------------------------
Un match est identifié par, dans cet ordre :
  (a) la fenêtre de coup d'envoi, ±KICKOFF_TOLERANCE_MIN ;
  (b) la ligue, après passage par la carte canonique ;
  (c) la proximité de la STRUCTURE de cotes — le vecteur de probabilités
      dévigorisées, qui n'a pas de langue.
Le nom d'équipe n'intervient qu'en (d), pour confirmer ou contredire. Cette
hiérarchie est le contraire de celle du harvester historique, et c'est
délibéré : (a)+(b)+(c) sont invariants par traduction, (d) ne l'est pas.

LA DIVERGENCE SE MESURE EN POINTS DE PROBABILITÉ, PAS EN POURCENT RELATIF
-------------------------------------------------------------------------
Le cahier des charges demandait « divergence > 3 % → SUSPECT_DATA ». Mesuré le
2026-08-22 sur Hull City–Manchester United, trois chemins indépendants
(500.com/Pinnacle, 500.com/Betfair, Polymarket) :

    écart relatif no-vig      1 (outsider à 9,5 %)   X        2 (favori)
    500/Pinnacle vs Polymkt        9,73 %          0,74 %      1,48 %
    500/Betfair  vs Polymkt        4,26 %          0,86 %      0,34 %

Les trois sources sont pourtant d'accord : en POINTS de probabilité, l'écart
maximal est de 1,07 point. Le relatif explose sur l'outsider parce qu'un tick
d'un cent à 0,095 pèse déjà 1,05 % relatif — un seuil relatif à 3 % marquerait
SUSPECT_DATA sur presque chaque outsider, c'est-à-dire exactement là où le
pipeline trouve ses edges. On garde donc la magnitude choisie par l'opérateur
(2/3) mais en points absolus, qui sont stables sur toute la plage de prix.
"""
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

log = logging.getLogger("PREDATOR.source_adapter")

# ── Rôles ────────────────────────────────────────────────────────────
# sharp     : prix de référence, sert de fair price (Pinnacle, exchanges)
# soft      : prix jouable, celui qu'on parie (1xBet, Bet365…)
# consensus : troisième avis, ne crée jamais un signal seul
# scores    : résultats/settlement uniquement
# names     : dictionnaire de noms canoniques, aucun prix
ROLES = ("sharp", "soft", "consensus", "scores", "names")

KICKOFF_TOLERANCE_MIN = int(os.environ.get("SOURCE_KICKOFF_TOL_MIN", "15"))

# Distance maximale entre deux vecteurs de probabilités dévigorisées pour que
# deux enregistrements puissent être LE MÊME match. Large exprès : c'est un
# filtre anti-absurdité (Chelsea–Everton ne ressemble pas à un match de D3
# argentine joué à la même minute), pas une mesure de qualité. La mesure de
# qualité, c'est SUSPECT_DIVERGENCE_PTS plus bas, et elle s'applique APRÈS
# l'appariement.
STRUCTURE_MAX_DISTANCE_PTS = float(os.environ.get("SOURCE_STRUCT_MAX_PTS", "12.0"))

# Divergence au-delà de laquelle deux chemins vers le MÊME prix de référence
# sont déclarés incohérents → SUSPECT_DATA, aucun signal. En points de
# probabilité (voir le docstring).
SUSPECT_DIVERGENCE_PTS = float(os.environ.get("SOURCE_SUSPECT_PTS", "3.0"))

# Écart minimal, en points, entre le meilleur candidat d'un appariement et le
# suivant. En dessous, les deux se valent et la paire est écartée plutôt que
# tranchée au hasard — voir le garde d'ambiguïté dans `pair_fixtures`.
AMBIGUITY_MARGIN_PTS = float(os.environ.get("SOURCE_AMBIGUITY_MARGIN_PTS", "2.0"))

# ── Mode ombre ───────────────────────────────────────────────────────
# Une source neuve enregistre et compare, mais ne crée aucun signal misable
# tant qu'elle n'a pas fait ses preuves. Reprend la logique de
# tests/test_shadow_mode.py : on ne coupe pas la collecte, on retire la
# recommandation — sinon on n'apprendrait jamais si la source est bonne.
SHADOW_MIN_MATCHES     = int(os.environ.get("SOURCE_SHADOW_MIN_MATCHES", "100"))
SHADOW_MAX_MEDIAN_PTS  = float(os.environ.get("SOURCE_SHADOW_MAX_MED_PTS", "2.0"))

# Ordre d'appel par scan — la discipline de quota de la mission 1 reste la
# règle : le gratuit vient en repli du Tier 1 payant, jamais à sa place, et la
# recherche web (budget IA) reste le dernier recours.
CALL_ORDER = ("odds_api", "titan007", "matchbook",
              "odds_api_io", "prediction_markets", "web_search")


@dataclass(frozen=True)
class SourceSpec:
    """Carte d'identité d'un adaptateur — déclarative, lue par ops.py et le
    scorecard. `trust` est une valeur INITIALE : le scorecard la rétrograde."""
    name: str
    role: str
    trust: float
    daily_budget: int
    langs: tuple = ("en",)
    quota_bucket: str = ""
    host: str = ""
    legal: str = ""

    def __post_init__(self):
        if self.role not in ROLES:
            raise ValueError(f"rôle inconnu: {self.role!r} (attendu {ROLES})")
        if not 0.0 <= self.trust <= 1.0:
            raise ValueError(f"trust hors [0,1]: {self.trust}")

    @property
    def bucket(self) -> str:
        return self.quota_bucket or self.name


# ── Probabilités ─────────────────────────────────────────────────────

def novig_probs(odds: list) -> list:
    """Cotes décimales → probabilités dévigorisées (somme 1), en FRACTION.

    Dévig multiplicatif : c'est le seul qui soit défini pour un nombre
    quelconque d'issues et qui ne suppose rien sur la forme du surround.
    core/math_engine.devig() (power/Shin) reste la référence pour le calcul
    d'edge ; ici on ne cherche qu'une SIGNATURE comparable entre sources, et
    elle doit être identique quelle que soit la source, donc simple.
    """
    if not odds:
        return []
    # TOUTES les pattes doivent être valides. Un 1X2 amputé de son nul
    # rendrait sinon une signature à deux issues — indiscernable d'un vrai
    # moneyline, et donc appariable avec lui par `structure_distance`. C'est
    # le scénario qui lie des cotes au mauvais match sans rien logger.
    if any(not isinstance(o, (int, float)) or o <= 1.01 for o in odds):
        return []
    qs = [1.0 / o for o in odds]
    total = sum(qs)
    return [q / total for q in qs] if total > 0 else []


def vig_pct(odds: list) -> float:
    """Marge du book, en %. Signature sans langue d'un bookmaker : Pinnacle
    tourne à 2-4 % sur du 1X2, un exchange sous 1 %, 皇冠/澳门 au-dessus de
    10 %. C'est ce qui a permis d'identifier les books de 500.com dont le nom
    est masqué pour un visiteur anonyme (voir core/odds500.py)."""
    qs = [1.0 / o for o in odds if isinstance(o, (int, float)) and o > 1.01]
    return (sum(qs) - 1.0) * 100 if qs else 0.0


def divergence_pts(odds_a: list, odds_b: list) -> float:
    """Écart maximal entre deux prix pour le même marché, en POINTS de
    probabilité dévigorisée. Rend -1.0 si la comparaison n'a pas de sens
    (arités différentes, prix invalides) — un appelant ne doit jamais lire
    « 0 point d'écart » là où il n'y a pas eu de comparaison."""
    pa, pb = novig_probs(odds_a), novig_probs(odds_b)
    if not pa or not pb or len(pa) != len(pb):
        return -1.0
    return max(abs(a - b) for a, b in zip(pa, pb)) * 100


def cross_check(prices_by_path: dict, threshold: float | None = None) -> tuple:
    """Consensus sharp multi-source.

    `prices_by_path` : {"odds_api": [2.10, 3.40, 3.30], "odds500": [...]} —
    le MÊME marché vu par des chemins indépendants.

    Rend (ok, worst_pts, detail). `ok=False` ⇒ SUSPECT_DATA : un des chemins
    porte un prix périmé, et on ne sait pas lequel. Ne rien émettre est le
    seul choix sûr — un edge né d'un prix périmé a exactement l'allure d'un
    bon edge.

    Un seul chemin ⇒ (True, -1.0, …) : rien à croiser n'est pas une anomalie,
    et surtout pas une confirmation. Le -1.0 le dit explicitement.
    """
    thr = SUSPECT_DIVERGENCE_PTS if threshold is None else threshold
    paths = {k: v for k, v in (prices_by_path or {}).items() if novig_probs(v or [])}
    if len(paths) < 2:
        return True, -1.0, {"paths": list(paths), "compared": 0}

    worst, worst_pair = 0.0, ()
    names = sorted(paths)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            d = divergence_pts(paths[a], paths[b])
            if d > worst:
                worst, worst_pair = d, (a, b)
    ok = worst <= thr
    if not ok:
        log.warning("SUSPECT_DATA: %s vs %s divergent de %.2f pts (seuil %.2f)",
                    worst_pair[0], worst_pair[1], worst, thr)
    return ok, round(worst, 3), {"paths": names, "compared": len(names),
                                 "worst_pair": list(worst_pair)}


# ── Appariement ──────────────────────────────────────────────────────

def _as_utc(value) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        txt = value.strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(txt)
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return None


@dataclass
class Fixture:
    """Forme minimale commune. `odds` = 1X2 (ou moneyline à 2 issues) servant
    de signature structurelle ; `team_ids` = identifiants natifs de la source,
    sans langue, prioritaires sur les libellés."""
    source: str
    match_id: str
    kickoff: datetime | str
    league: str = ""
    home: str = ""
    away: str = ""
    odds: list = field(default_factory=list)
    team_ids: tuple = ()
    lang: str = "en"
    raw: dict = field(default_factory=dict)

    @property
    def kickoff_utc(self) -> datetime | None:
        return _as_utc(self.kickoff)


# Carte des ligues : libellés natifs → clé canonique. Volontairement PARTIELLE.
# Une ligue absente n'est pas une erreur : elle rend `league_key()` = "" et
# l'appariement se rabat sur (temps + structure), plus strict. Ajouter une
# entrée ne fait qu'AUTORISER un appariement, jamais l'imposer.
LEAGUE_MAP = {
    # 500.com (chinois) — Big 5 + portefeuille
    "英超": "epl", "英冠": "efl_championship", "西甲": "laliga", "意甲": "seriea",
    "德甲": "bundesliga", "法甲": "ligue1", "法乙": "ligue2", "荷甲": "eredivisie",
    "葡超": "primeira", "苏超": "spl", "瑞超": "allsvenskan", "挪超": "eliteserien",
    "美职足": "mls", "巴甲": "brasileirao", "巴乙": "brasileirao_b",
    "阿甲": "argentina_primera", "日职": "j_league", "日乙": "j_league_2",
    "韩职": "k_league", "欧冠": "ucl", "欧联": "uel", "德国杯": "dfb_pokal",
    "芬兰超级联赛": "veikkausliiga",
    # libellés anglais rencontrés chez 7M / Kalshi / Polymarket
    "english premier league": "epl", "premier league": "epl", "epl": "epl",
    "spanish la liga": "laliga", "la liga": "laliga", "laliga": "laliga",
    "italian serie a": "seriea", "serie a": "seriea",
    "german bundesliga": "bundesliga", "bundesliga": "bundesliga",
    "french ligue 1": "ligue1", "ligue 1": "ligue1",
    "uefa champions league": "ucl", "champions league": "ucl", "ucl": "ucl",
    "uefa europa league": "uel", "europa league": "uel", "uel": "uel",
    "major league soccer": "mls", "mls": "mls",
    "brazil serie a": "brasileirao", "brasileirao": "brasileirao",
    "j league": "j_league", "j1 league": "j_league",
    "k league": "k_league", "k league 1": "k_league",
    "nba": "nba", "nfl": "nfl", "mlb": "mlb", "euroleague": "euroleague",
    # libellés EXACTS relevés le 2026-08-22 sur le sitemap 7M (core/sevenm.py).
    # Recopiés depuis la source plutôt que devinés : c'est cette table qui
    # autorise l'appariement 500.com↔7M, donc la construction gratuite du
    # dictionnaire d'alias. Une entrée manquante ne casse rien — elle prive
    # seulement d'un appariement (voir `pair_fixtures`).
    "efl championship": "efl_championship", "efl league one": "efl_league_one",
    "efl league two": "efl_league_two", "national league": "national_league",
    "spanish segunda division": "laliga2", "primeira liga": "primeira",
    "liga portugal 2": "primeira_2", "italian serie b": "seriea_b",
    "dfb-pokal": "dfb_pokal", "austrian bundesliga": "austria_bundesliga",
    "campeonato brasileiro serie a": "brasileirao",
    "campeonato brasileiro serie b": "brasileirao_b",
    "argentine primera division": "argentina_primera",
    "primera nacional": "argentina_nacional",
    "uruguay primera division": "uruguay_primera",
    "venezuela primera division": "venezuela_primera",
    "bolivian primera division": "bolivia_primera",
    "categoria primera a": "colombia_primera",
    "copa sudamericana": "sudamericana", "copa libertadores": "libertadores",
    "saudi pro league": "saudi_pro", "uae pro league": "uae_pro",
    "qatar stars league": "qatar_stars", "turkish super lig": "turkish_super",
    "greece super league 1": "greece_super", "russian premier league": "russia_premier",
    "czech first league": "czech_first", "liga i": "romania_liga1",
    "usl championship": "usl_championship", "eredivisie": "eredivisie",
    # libellés EXACTS du calendrier titan007 (`core/titan007.py`), relevés le
    # 2026-08-28 sur 234 fixtures / 114 libellés. Ils servent au TRI par
    # priorité avant le cap de 40 (voir `league_rank`) : sans eux, le vendredi
    # soir, 58 coups d'envoi de 18:00 (U21, Welsh PR, Pologne D3…) remplissent
    # la fenêtre et Bayern–Stuttgart (18:30) reste en position 62.
    "eng pr": "epl", "ger d1": "bundesliga", "spa d1": "laliga", "ita d1": "seriea",
    "fra d1": "ligue1", "hol d1": "eredivisie", "por d1": "primeira", "bel d1": "belgium_pro",
    "tur d1": "turkish_super", "sco pr": "spl", "arg d1": "argentina_primera",
    "bra d1": "brasileirao", "usa mls": "mls", "jpn d1": "j_league", "kor d1": "k_league",
    "sau d1": "saudi_pro", "eng lch": "efl_championship", "ger d2": "bundesliga2",
    "spa d2": "laliga2", "ita d2": "seriea_b", "fra d2": "ligue2", "hol d2": "eredivisie2",
    "allsvenskan": "allsvenskan", "veikkausliiga": "veikkausliiga",
    "eliteserien": "eliteserien", "challenger pro league": "belgium_challenger",
}


def league_key(label: str) -> str:
    """Clé canonique d'une ligue, ou "" si inconnue. "" n'autorise aucun
    appariement par ligue — il force l'appariement à se justifier autrement.

    Correspondance EXACTE, volontairement. Élargir cette fonction élargirait
    l'appariement entre sources, où une clé fausse lie le prix d'un match aux
    cotes d'un autre — voir `league_key_correlation` pour l'usage où l'erreur
    a le sens inverse."""
    if not label:
        return ""
    raw = label.strip()
    return LEAGUE_MAP.get(raw) or LEAGUE_MAP.get(raw.lower(), "")


_SEPARATEURS = ("|", ":", "–", "—", " - ")


def league_key_correlation(label: str) -> str:
    """Clé de ligue pour le TAG DE CORRÉLATION seulement — plus large que
    `league_key`, et c'est délibéré.

    Mesuré le 2026-09-04 : deux signaux de Ligue 1 du MÊME jour portaient des
    tags différents, « soccer:2026-09-04:Ligue 1 - France » (OddsAPI) et
    « soccer:2026-09-04:FRA D1 » (Tier 2). `core.tax_engine` en concluait deux
    paris indépendants et acceptait de les combiner, alors qu'ils partagent
    journée, arbitrage, météo et calendrier. `LEAGUE_MAP` connaissait pourtant
    « fra d1 » ET « ligue 1 » : c'est la forme « Ligue - Pays » qui échappait à
    la correspondance exacte.

    L'ASYMÉTRIE qui autorise cet élargissement : le tag n'a qu'un consommateur,
    `tax_engine._combine_with_correlation`, en mode « forbid » — deux jambes qui
    partagent un tag sont REFUSÉES. Sur-grouper coûte donc une combinaison
    écartée ; sous-grouper fait passer une corrélation pour de l'indépendance et
    gonfle la probabilité annoncée. On préfère franchement le premier.

    Conséquence assumée : « Brazil - Serie A » tombe sur `seriea` comme
    l'italien. Deux championnats réellement distincts sont alors refusés au
    combiné — un coût nul pour une garde. Ne PAS « corriger » ça en revenant à
    la correspondance exacte sans relire le paragraphe ci-dessus.

    Une ligue inconnue de la carte garde son libellé normalisé : elle se groupe
    donc avec elle-même, jamais avec une autre.
    """
    exact = league_key(label)
    if exact:
        return exact
    brut = (label or "").strip()
    if not brut:
        return ""
    decoupe = brut
    for sep in _SEPARATEURS:
        decoupe = decoupe.replace(sep, "\n")
    for segment in decoupe.split("\n"):
        trouve = league_key(segment.strip())
        if trouve:
            return trouve
    return " ".join(brut.lower().split())


# Ligues servies EN PREMIER quand une source coupe son calendrier (titan007 :
# 40 matchs sur ~240 à 24 h). Ordre = liquidité du marché sharp, donc qualité
# du prix de référence — pas une préférence sportive. Tout ce qui n'y figure
# pas passe APRÈS, dans l'ordre des coups d'envoi comme avant : la liste ne
# retire rien, elle réordonne.
LEAGUE_PRIORITY = (
    "epl", "laliga", "seriea", "bundesliga", "ligue1",
    "ucl", "uel",
    "eredivisie", "primeira", "belgium_pro", "turkish_super", "spl",
    "efl_championship", "bundesliga2", "laliga2", "seriea_b", "ligue2",
    "mls", "brasileirao", "argentina_primera", "j_league", "k_league", "saudi_pro",
    "libertadores", "sudamericana",
)
_RANK = {k: i for i, k in enumerate(LEAGUE_PRIORITY)}


def league_rank(label: str) -> int:
    """Rang de priorité d'un libellé de ligue : 0 = servi en premier ;
    `len(LEAGUE_PRIORITY)` pour tout ce qui est inconnu ou hors liste."""
    return _RANK.get(league_key(label), len(LEAGUE_PRIORITY))


def detect_lang(text: str) -> str:
    """Langue d'un libellé d'équipe, par plage Unicode. Sert à router la
    résolution d'alias (7M gratuit, puis IA), pas à traduire quoi que ce soit."""
    if not text:
        return "en"
    has_kana = has_hangul = has_han = False
    for ch in text:
        code = ord(ch)
        if 0x3040 <= code <= 0x30FF:
            has_kana = True
        elif 0xAC00 <= code <= 0xD7AF or 0x1100 <= code <= 0x11FF:
            has_hangul = True
        elif 0x4E00 <= code <= 0x9FFF or 0x3400 <= code <= 0x4DBF:
            has_han = True
    if has_hangul:
        return "ko"
    if has_kana:
        return "ja"       # les kana tranchent : du han seul reste du chinois
    if has_han:
        return "zh"
    return "en"


def structure_distance(a: Fixture, b: Fixture) -> float:
    """Distance entre les signatures de cotes, en points. -1.0 si l'une des
    deux n'a pas de cotes exploitables : « pas comparable » n'est pas
    « identique »."""
    return divergence_pts(a.odds, b.odds)


def pair_fixtures(left: list, right: list,
                  tolerance_min: int | None = None,
                  max_structure_pts: float | None = None,
                  require_league: bool = False) -> list:
    """Apparie deux calendriers SANS se fier aux noms.

    `require_league=True` : la ligue doit être CONNUE des deux côtés et
    concorder — une ligue inconnue d'un côté n'est plus « pas de désaccord »,
    c'est « pas de preuve ». Mesuré le 2026-08-28 15:48 sur l'appariement
    odds500↔slate de confiance (28×104) : 5 paires, dont 4 FAUSSES — 拜仁 et
    斯图加特 (Bundesliga) appris comme UCD et Finn Harps (Irlande D2), 蒙彼利埃
    et 布洛涅 (Ligue 2) comme Farsta et Nacka Iliria, 雷克斯 et 伯明翰
    (Championship) comme Kerry et Treaty United, 卡斯鲁厄 et 沃夫斯堡 comme
    AIK W et Kristianstad W. Même minute, gros favori des deux côtés (moins
    de 12 pts d'écart), et un libellé api-sports absent de LEAGUE_MAP : la
    garde `la and lb and la != lb` ne pouvait rien refuser. Ces alias
    partaient à 0,7 — utilisables dès l'écriture. Sur ce chemin-là, le temps
    et la structure ne suffisent pas ; on exige la ligue.

    Rend une liste de (fixture_gauche, fixture_droite, evidence). `evidence`
    porte l'écart de coup d'envoi, la clé de ligue et la distance structurelle
    — c'est ce que le dictionnaire d'alias consomme pour monter ou invalider
    une confiance (core/team_aliases.py).

    Appariement GLOUTON par meilleure distance : chaque fixture n'est utilisée
    qu'une fois. Un match ambigu (deux candidats aussi proches) est laissé
    non apparié plutôt que tranché au hasard — apparier la mauvaise équipe
    produit un edge crédible et faux, ne rien apparier ne coûte qu'un match.
    """
    tol = timedelta(minutes=KICKOFF_TOLERANCE_MIN if tolerance_min is None else tolerance_min)
    max_pts = STRUCTURE_MAX_DISTANCE_PTS if max_structure_pts is None else max_structure_pts

    candidates = []
    for i, a in enumerate(left):
        ka = a.kickoff_utc
        if ka is None:
            continue
        for j, b in enumerate(right):
            kb = b.kickoff_utc
            if kb is None or abs(ka - kb) > tol:
                continue
            la, lb = league_key(a.league), league_key(b.league)
            if la and lb and la != lb:
                continue                      # deux ligues connues et différentes
            if require_league and not (la and lb and la == lb):
                continue                      # ligue inconnue d'un côté : pas de preuve
            dist = structure_distance(a, b)
            if dist > max_pts:
                continue                      # structures incompatibles
            # Sans cotes des deux côtés (dist == -1), il ne reste que le temps
            # et la ligue : on n'accepte QUE si la ligue est connue des deux
            # côtés et concorde. Sinon deux matchs simultanés d'une même
            # soirée seraient interchangeables.
            if dist < 0 and not (la and lb and la == lb):
                continue
            score = (dist if dist >= 0 else max_pts,
                     abs((ka - kb).total_seconds()))
            candidates.append((score, i, j, dist, la or lb))

    candidates.sort()

    # AMBIGUÏTÉ — le garde qui manquait. Mesuré le 2026-08-22 sur 64 fixtures
    # 500.com × 451 fixtures 7M : 16 paires, dont UNE fausse — 斯旺西/谢菲联
    # (Swansea/Sheffield Utd) apparié à Wrexham/Watford. Les deux matchs sont
    # en EFL Championship à la MÊME minute, et les calendriers ne portent pas
    # de cotes : (a) et (b) ne distinguent rien, (c) est indisponible. Le tri
    # glouton tranchait alors au hasard — et écrivait un alias faux, c'est-à-dire
    # exactement l'erreur qui produit un edge crédible et imaginaire.
    #
    # Règle : un appariement doit être JUSTIFIÉ, pas seulement le meilleur.
    #   - sans signature de cotes, on exige l'UNICITÉ (un seul candidat de
    #     chaque côté) ;
    #   - avec signature, on exige que le second candidat soit nettement plus
    #     loin (AMBIGUITY_MARGIN_PTS).
    # Un match ambigu est laissé NON APPARIÉ : ça ne coûte qu'un match, là où
    # une fausse paire empoisonne le dictionnaire d'alias à vie.
    by_left: dict = {}
    by_right: dict = {}
    for score, i, j, dist, lk in candidates:
        by_left.setdefault(i, []).append((score, dist))
        by_right.setdefault(j, []).append((score, dist))

    used_l, used_r, pairs = set(), set(), []
    for score, i, j, dist, lk in candidates:
        if i in used_l or j in used_r:
            continue
        if dist < 0:
            if len(by_left[i]) > 1 or len(by_right[j]) > 1:
                log.debug("appariement ambigu sans cotes (%d candidats) — écarté",
                          max(len(by_left[i]), len(by_right[j])))
                continue
        else:
            rivaux = [d for sc, d in by_left[i] if sc != score] + \
                     [d for sc, d in by_right[j] if sc != score]
            proches = [d for d in rivaux if d >= 0 and d - dist < AMBIGUITY_MARGIN_PTS]
            if proches:
                log.debug("appariement ambigu (%d rival(aux) à moins de %.1f pts) — écarté",
                          len(proches), AMBIGUITY_MARGIN_PTS)
                continue
        used_l.add(i)
        used_r.add(j)
        a, b = left[i], right[j]
        pairs.append((a, b, {
            "kickoff_delta_s": int(abs((a.kickoff_utc - b.kickoff_utc).total_seconds())),
            "league_key": lk,
            "structure_pts": round(dist, 3) if dist >= 0 else None,
        }))
    log.info("appariement %s↔%s : %d paires sur %d×%d",
             left[0].source if left else "?", right[0].source if right else "?",
             len(pairs), len(left), len(right))
    return pairs


# ── Scorecard par source (table `meta`) ──────────────────────────────
# Symétrique de la boucle d'apprentissage : on MESURE en continu, on logge, et
# la rétrogradation est explicite. Rien n'est appliqué en silence.

_SCORECARD_KEY = "source_scorecard_{name}"


def _db():
    try:
        from core.db import get_db
        return get_db(write=True)
    except Exception as e:
        log.debug("scorecard: pas de base (%s)", e)
        return None


def load_scorecard(name: str) -> dict:
    """Scorecard courante, ou un gabarit vide. Ne lève jamais."""
    empty = {"source": name, "matched": 0, "errors": 0, "requests": 0,
             "divergence_samples": [], "median_divergence_pts": None,
             "freshness_median_s": None, "trust": None, "role": None,
             "shadow": True, "promoted_at": None, "demoted_at": None}
    sb = _db()
    if sb is None:
        return empty
    try:
        row = sb.table("meta").select("value").eq(
            "key", _SCORECARD_KEY.format(name=name)).maybe_single().execute()
        if row and row.data and row.data.get("value"):
            return {**empty, **json.loads(row.data["value"])}
    except Exception as e:
        log.debug("scorecard[%s]: lecture impossible (%s)", name, e)
    return empty


def save_scorecard(card: dict) -> None:
    sb = _db()
    if sb is None:
        return
    try:
        sb.table("meta").upsert(
            {"key": _SCORECARD_KEY.format(name=card["source"]),
             "value": json.dumps(card, ensure_ascii=False),
             "updated_at": datetime.now(timezone.utc).isoformat()},
            on_conflict="key").execute()
    except Exception as e:
        log.debug("scorecard[%s]: écriture impossible (%s)", card.get("source"), e)


def _median(values: list) -> float | None:
    vals = sorted(v for v in values if isinstance(v, (int, float)) and v >= 0)
    if not vals:
        return None
    mid = len(vals) // 2
    return vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2


# On ne garde qu'une fenêtre glissante d'échantillons : un scorecard doit
# refléter l'état ACTUEL de la source. Une source qui a dérivé il y a trois
# mois puis s'est corrigée ne doit pas rester rétrogradée à vie.
SCORECARD_WINDOW = int(os.environ.get("SOURCE_SCORECARD_WINDOW", "300"))


def record_observation(card: dict, divergence_pts_value: float | None = None,
                       freshness_s: float | None = None,
                       matched: int = 0, errors: int = 0,
                       requests: int = 0) -> dict:
    """Ajoute une observation au scorecard (fonction pure — l'appelant sauve)."""
    card = dict(card)
    card["matched"]  = int(card.get("matched") or 0) + matched
    card["errors"]   = int(card.get("errors") or 0) + errors
    card["requests"] = int(card.get("requests") or 0) + requests

    if divergence_pts_value is not None and divergence_pts_value >= 0:
        samples = list(card.get("divergence_samples") or [])
        samples.append(round(float(divergence_pts_value), 3))
        card["divergence_samples"] = samples[-SCORECARD_WINDOW:]
        card["median_divergence_pts"] = _median(card["divergence_samples"])

    if freshness_s is not None and freshness_s >= 0:
        fresh = list(card.get("freshness_samples") or [])
        fresh.append(round(float(freshness_s), 1))
        card["freshness_samples"] = fresh[-SCORECARD_WINDOW:]
        card["freshness_median_s"] = _median(card["freshness_samples"])
    return card


def evaluate_promotion(card: dict, spec: SourceSpec) -> tuple:
    """Une source sort-elle du mode ombre ?

    Rend (shadow_after, verdict, detail). Les deux conditions du cahier des
    charges, ET aucune autre : au moins SHADOW_MIN_MATCHES matchs appariés, et
    une divergence MÉDIANE sous SHADOW_MAX_MEDIAN_PTS face à une source de
    confiance. La médiane, pas la moyenne : un seul prix périmé ne doit ni
    bloquer une bonne source, ni sauver une mauvaise.

    Une source déjà promue qui dérive est RÉTROGRADÉE ici même — c'est
    l'asymétrie voulue : la promotion demande 100 matchs, la rétrogradation
    est immédiate.
    """
    matched = int(card.get("matched") or 0)
    med = card.get("median_divergence_pts")
    shadow = bool(card.get("shadow", True))

    if med is None:
        return True, "insuffisant", {"matched": matched, "median_pts": None,
                                     "reason": "aucune divergence mesurée"}
    if not shadow and med > SHADOW_MAX_MEDIAN_PTS:
        return True, "rétrogradée", {"matched": matched, "median_pts": med,
                                     "reason": f"divergence médiane {med:.2f} > {SHADOW_MAX_MEDIAN_PTS} pts"}
    if shadow and matched >= SHADOW_MIN_MATCHES and med <= SHADOW_MAX_MEDIAN_PTS:
        return False, "promue", {"matched": matched, "median_pts": med,
                                 "reason": f"{matched} matchs, divergence médiane {med:.2f} pts"}
    if shadow:
        return True, "en ombre", {"matched": matched, "median_pts": med,
                                  "reason": f"{matched}/{SHADOW_MIN_MATCHES} matchs, médiane {med:.2f} pts"}
    return False, "confirmée", {"matched": matched, "median_pts": med, "reason": "ok"}


def effective_trust(card: dict, spec: SourceSpec) -> float:
    """Confiance réellement applicable : la valeur déclarée, divisée par deux
    tant que la source est en ombre. Une source en ombre ne crée aucun signal
    misable (c'est run_engine qui l'applique) ; cette valeur sert au poids
    qu'elle prend dans un consensus, où sa présence reste informative."""
    trust = card.get("trust")
    base = float(trust) if isinstance(trust, (int, float)) else spec.trust
    return round(base * (0.5 if card.get("shadow", True) else 1.0), 3)
