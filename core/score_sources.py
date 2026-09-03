"""
core/score_sources.py — scores finals STRUCTURÉS, sans aucune IA (2026-09-02).

POURQUOI CE MODULE. Jusqu'au 2026-09-02, le repli du settlement était une
recherche web (Groq compound-mini + Tavily) : deux quotas gratuits qui ont
lâché ENSEMBLE deux fois en une semaine (2026-08-26 et 2026-09-01, « AUDIT
STÉRILE — 0 réglé sur 3 éligibles »). Un score est une DONNÉE STRUCTURÉE
publiée par des API gratuites ; le demander à un LLM, c'est payer deux quotas
et un parseur de JSON approximatif pour une information qui existe en champ.
Décision opérateur du 2026-09-02 : Groq et Tavily sont SUPPRIMÉS du pipeline,
le settlement est déterministe de bout en bout.

TROIS SOURCES, APRÈS api-sports (qui reste l'étage 1, dans core/settlement.py) :

  1. MLB statsapi (statsapi.mlb.com) — officiel, sans clé, une requête par
     journée pour tout le slate. Ne sert que le baseball MLB.
  1bis. ESPN (site.api.espn.com, 2026-09-03) — scoreboard public SANS CLÉ,
     ni compte, ni quota publié. `soccer/all` rend TOUTES les ligues de foot
     du monde en UNE requête par fenêtre de dates (mesuré : 702 événements
     terminés sur 3 jours, de la Premier League à la J.League) ; les autres
     sports ont un slug par compétition (`_ESPN_PATHS`). Décision opérateur :
     « pour les résultats chercher sources open, pas TheSportsDB » — ESPN
     passe AVANT TheSportsDB, qui reste en dernier recours. Statut TERMINÉ =
     `status.type.completed` ET `state == "post"`. Les sports d'athlètes
     (MMA, boxe, tennis) n'ont pas de score par équipe sur ce flux : absents
     de la table, ils sautent cette voie.
  2. TheSportsDB — clé publique gratuite « 123 » (surchargée par
     THESPORTSDB_API_KEY pour un compte Patreon). ⚠️ La requête « tous les
     matchs du jour » (eventsday) est PLAFONNÉE À 3 ÉVÉNEMENTS en gratuit —
     mesuré le 2026-09-02 — donc inutilisable. La voie qui marche est PAR
     ÉQUIPE : searchteams → eventslast, qui a retrouvé du premier coup les
     deux signaux en souffrance du jour (Hapoel Akko 0-3 Bnei Yehuda, ligue
     israélienne D2 — FT).
     ⚠️ PLAFONDS de la clé gratuite, mesurés le 2026-09-02 et confirmés par
     la doc officielle : eventslast rend UN seul résultat (10 en premium) et
     searchteams UNE seule équipe (100 en premium). Dès qu'une équipe a
     rejoué, le match du signal sort donc de la fenêtre et devient
     introuvable par cette voie — cause n°1 des `expired` de ligues mineures,
     que le compte Patreon (~9 $/mois) lèverait sans toucher au code.

LE MÊME CONTRAT QUE result_from_api_sports, ET IL NE SE DISCUTE PAS :
  - appariement `strict_team_match` sur les DEUX équipes du MÊME événement.
    La recherche d'équipe seule ne suffit PAS : mesuré le 2026-09-02,
    searchteams("AD Pasto") rend « Pastoreo » (sans ligue) et
    strict_team_match les accepte par containment. C'est l'événement complet
    (les deux noms + la date) qui décide, jamais un nom isolé ;
  - candidat UNIQUE exigé : deux prétendants → REFUS. Un WIN/LOSS faux au
    ledger est définitif, l'attente ne l'est pas ;
  - seuls les statuts TERMINÉS règlent. TheSportsDB publie des scores en
    direct : régler sur un score à la 70e minute écrirait un résultat faux ;
  - toute panne réseau → None + log, jamais d'exception (convention dépôt).

BUDGETS. Compteurs partagés `daily_quota` (comme toute source), SANS rythme
horaire : ces fonctions ne servent que le settlement, et l'incident du
2026-08-28 a établi qu'étaler le settlement est une faute — un match déjà
joué ne se règle pas mieux plus tard, il sort en `expired`.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from core import daily_quota, net
from core.paim_engine import strict_team_match

log = logging.getLogger("PREDATOR.score_sources")

MLB_URL = "https://statsapi.mlb.com/api/v1/schedule"
ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"
TSDB_BASE = "https://www.thesportsdb.com/api/v1/json"

# Clé publique documentée par TheSportsDB pour les tests/le gratuit. Un compte
# Patreon la remplace par THESPORTSDB_API_KEY (plus de requêtes, eventsday
# complet) sans toucher au code.
_TSDB_PUBLIC_KEY = "123"

# Budgets journaliers PRUDENTS côté PREDATOR (leçon api-sports, compte
# suspendu le 2026-08-20 : on bascule avant de se faire couper). statsapi ne
# publie pas de limite ; TheSportsDB gratuit tolère ~30 req/min.
MLB_DAILY_BUDGET = int(os.environ.get("MLB_STATSAPI_DAILY_BUDGET", "80"))
# ESPN ne publie aucune limite ; une requête couvre une fenêtre de 3 jours et
# TOUT un sport, donc un audit en consomme quelques-unes. Borne prudente.
ESPN_DAILY_BUDGET = int(os.environ.get("ESPN_DAILY_BUDGET", "400"))
TSDB_DAILY_BUDGET = int(os.environ.get("THESPORTSDB_DAILY_BUDGET", "150"))
_MLB_BUCKET = "mlb_results"
_ESPN_BUCKET = "espn_results"
_TSDB_BUCKET = "tsdb_results"

# Sport interne → chemins de scoreboard ESPN à interroger, dans l'ordre.
# `soccer/all` couvre toutes les ligues de football en une requête ; les
# autres sports n'ont pas d'agrégat et se listent par compétition (vérifiés
# un à un le 2026-09-03 : chacun rend des événements terminés avec score).
# Un sport absent saute la voie ESPN (MMA/boxe/tennis : athlètes, pas
# d'équipes ni de score au sens de ce module).
_ESPN_PATHS = {
    "soccer":                ["soccer/all"],
    "basketball":            ["basketball/nba", "basketball/wnba"],
    "euroleague_basketball": ["basketball/euroleague"],
    "hockey":                ["hockey/nhl"],
    "baseball":              ["baseball/mlb"],
    "americanfootball":      ["football/nfl"],
    "college_football":      ["football/college-football"],
    "aussierules":           ["australian-football/afl"],
    "rugbyleague":           ["rugby-league/3"],          # NRL
    # MMA : une carte = un événement, chaque combat = une « competition » à
    # deux ATHLÈTES (pas de homeAway) avec un drapeau `winner` une fois
    # terminé. Réglé 1-0 / 0-1 dans l'ordre des combattants du signal.
    "mma":                   ["mma/ufc"],
}
# User-Agent ESPN. MESURÉ le 2026-09-03 depuis ce Codespace : « PREDATOR/1.0 »
# et un UA de navigateur reçoivent 403, « curl/8.5.0 », « Python-urllib/3.11 »
# et un UA de la forme « produit/version (+url) » reçoivent 200. Le pare-feu
# d'ESPN filtre donc sur la FORME de l'en-tête, pas sur l'adresse (les runners
# GitHub sont sur Azure comme ce Codespace). Surchargeable par l'env si la
# règle change encore — sans redéploiement.
_ESPN_USER_AGENT = os.environ.get(
    "ESPN_USER_AGENT", "predator-settlement/1.0 (+https://github.com/ipotrading-bot/predator)")
_SOURCE_HEADERS = {"espn": {"User-Agent": _ESPN_USER_AGENT}}

# Fenêtre de dates sans `match_date` (relance d'une ligne de ledger sans
# date) : les N derniers jours. Court, sinon la paire unique exigée devient
# ambiguë dès qu'un même duo a rejoué.
_ESPN_JOURS_SANS_DATE = 3

_TIMEOUT = int(os.environ.get("SCORE_SOURCES_TIMEOUT", "15"))

# Requêtes par SIGNAL sur la voie par équipe : 2 searchteams + au plus 4
# eventslast. Sans ce plafond, un slate de 25 signaux introuvables viderait le
# budget du jour sur des recherches d'équipes fantômes.
_TSDB_TEAMS_PER_SIDE = 2

# Statuts TERMINÉS. TheSportsDB n'a pas de vocabulaire unique : le football
# rend « FT »/« AET »/« PEN », d'autres sports « Match Finished » ou « Final ».
# Un statut ABSENT ne règle PAS (score possiblement en direct) — la ligne
# repassera au prochain audit, et l'attente est le comportement correct.
_TSDB_FINISHED = frozenset({"ft", "aet", "pen", "match finished", "finished", "final"})

# Sport interne → libellé strSport de TheSportsDB. Un sport absent de cette
# table passe la voie TSDB sans filtre de sport (l'appariement des deux noms
# + la date reste seul juge) — mieux qu'une table qu'on croit exhaustive.
_TSDB_SPORTS = {
    "soccer": "Soccer",
    "basketball": "Basketball",
    "euroleague_basketball": "Basketball",
    "baseball": "Baseball",
    "hockey": "Ice Hockey",
    "americanfootball": "American Football",
    "college_football": "American Football",
    "rugbyleague": "Rugby",
    "aussierules": "Australian Football",
    "mma": "Fighting",
    "boxing": "Fighting",
    "tennis": "Tennis",
}

# Caches de RUN (un audit règle des dizaines de matchs de la même journée).
_CACHE_MLB: dict[str, list] = {}
_CACHE_ESPN: dict[tuple[str, str], list] = {}
_CACHE_TSDB_TEAMS: dict[str, list] = {}
_CACHE_TSDB_EVENTS: dict[str, list] = {}


def reset_cache() -> None:
    """Vide les caches de run (tests, runs longs)."""
    _CACHE_MLB.clear()
    _CACHE_ESPN.clear()
    _CACHE_TSDB_TEAMS.clear()
    _CACHE_TSDB_EVENTS.clear()


def _get_json(url: str, bucket: str, budget: int, source: str | None = None) -> dict | None:
    """GET JSON avec budget partagé. None sur toute panne — jamais d'exception.

    `source` (ex. « espn ») route la requête par `core.net` : relais ou proxy
    `{SOURCE}_PROXY` / `FREE_SOURCES_PROXY` s'ils sont configurés — la
    parade documentée si les runners GitHub se font refuser (leçon
    ESPN/SofaScore, INCIDENTS.md). Sans `source`, chemin direct inchangé."""
    if daily_quota.spent(bucket) >= budget:
        log.warning("score_sources[%s]: budget journalier atteint (%d) — requête sautée",
                    bucket, budget)
        return None
    try:
        headers = dict(_SOURCE_HEADERS.get(source or "", {"User-Agent": "PREDATOR/1.0"}))
        if source:
            url_reelle, headers = net.prepare(source, url, headers)
            req = urllib.request.Request(url_reelle, headers=headers)
            with net.open_with_retry(source, req, _TIMEOUT) as resp:
                body = resp.read()
        else:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                body = resp.read()
        daily_quota.add(bucket, 1)
        return json.loads(body.decode("utf-8", errors="replace"))
    except Exception as e:                                        # noqa: BLE001
        daily_quota.add(bucket, 1)
        log.warning("score_sources[%s]: %s — %s", bucket, url.split("?")[0], e)
        return None


def _split(match_name: str) -> tuple[str, str] | None:
    if " vs " not in (match_name or ""):
        return None
    home, away = (p.strip() for p in match_name.split(" vs ", 1))
    if len(home) < 3 or len(away) < 3:
        return None
    return home, away


def _jours(match_date: str) -> list[str]:
    """[jour, lendemain, veille] — un coup d'envoi tardif bascule de journée UTC."""
    if not match_date:
        return []
    try:
        d = datetime.fromisoformat(match_date)
    except ValueError:
        return [match_date]
    return [match_date,
            (d + timedelta(days=1)).strftime("%Y-%m-%d"),
            (d - timedelta(days=1)).strftime("%Y-%m-%d")]


# ── 1. MLB statsapi ───────────────────────────────────────────────────

def _mlb_du_jour(jour: str) -> list:
    if jour not in _CACHE_MLB:
        data = _get_json(f"{MLB_URL}?sportId=1&date={jour}", _MLB_BUCKET, MLB_DAILY_BUDGET)
        rows = []
        for day in (data or {}).get("dates", []):
            for g in day.get("games", []):
                st = (g.get("status") or {}).get("abstractGameState", "")
                teams = g.get("teams") or {}
                home = (teams.get("home") or {})
                away = (teams.get("away") or {})
                rows.append({
                    "home": ((home.get("team") or {}).get("name") or ""),
                    "away": ((away.get("team") or {}).get("name") or ""),
                    "home_score": home.get("score"),
                    "away_score": away.get("score"),
                    "final": st == "Final",
                })
        _CACHE_MLB[jour] = rows
    return _CACHE_MLB[jour]


def result_from_mlb(match_name: str, match_date: str) -> dict | None:
    """Score final MLB depuis l'API officielle. Baseball seulement.

    Même contrat qu'api-sports : deux noms appariés, candidat unique, et seul
    `abstractGameState == "Final"` règle (statsapi publie les scores en live).
    """
    parts = _split(match_name)
    if not parts or not match_date:
        return None
    home, away = parts
    for jour in _jours(match_date):
        rows = _mlb_du_jour(jour)
        candidats = [r for r in rows
                     if r["final"] and r["home_score"] is not None
                     and r["away_score"] is not None
                     and strict_team_match(home, r["home"])
                     and strict_team_match(away, r["away"])]
        if len(candidats) == 1:
            r = candidats[0]
            log.info("SETTLE mlb_statsapi | %s | %d-%d (0 appel IA)",
                     match_name, int(r["home_score"]), int(r["away_score"]))
            return {"home_score": int(r["home_score"]), "away_score": int(r["away_score"]),
                    "completed": True, "source": "mlb_statsapi"}
        if len(candidats) > 1:
            log.info("SETTLE SKIP | %s — %d matchs MLB correspondent, on ne devine pas",
                     match_name, len(candidats))
            return None
    return None


# ── 1bis. ESPN (scoreboard public, sans clé) ─────────────────────────

def _espn_fenetre(match_date: str) -> str | None:
    """`dates=AAAAMMJJ-AAAAMMJJ` : veille → lendemain autour de la date, ou
    les _ESPN_JOURS_SANS_DATE derniers jours sans date."""
    if match_date:
        try:
            d = datetime.fromisoformat(match_date)
        except ValueError:
            return None
        debut, fin = d - timedelta(days=1), d + timedelta(days=1)
    else:
        fin = datetime.utcnow()
        debut = fin - timedelta(days=_ESPN_JOURS_SANS_DATE)
    return f"{debut:%Y%m%d}-{fin:%Y%m%d}"


def _espn_events(path: str, fenetre: str) -> list:
    cle = (path, fenetre)
    if cle not in _CACHE_ESPN:
        url = f"{ESPN_BASE}/{path}/scoreboard?dates={fenetre}&limit=1000"
        data = _get_json(url, _ESPN_BUCKET, ESPN_DAILY_BUDGET, source="espn") or {}
        _CACHE_ESPN[cle] = list(data.get("events") or [])
    return _CACHE_ESPN[cle]


def _espn_noms(competitor: dict) -> list[str]:
    """Les libellés sous lesquels ESPN désigne une équipe (displayName,
    shortDisplayName, name, location) ou un athlète (fullName, displayName,
    shortName) — l'appariement strict en essaie un."""
    team = competitor.get("team") or {}
    ath = competitor.get("athlete") or {}
    return [n for n in (team.get("displayName"), team.get("shortDisplayName"),
                        team.get("name"), team.get("location"),
                        ath.get("fullName"), ath.get("displayName"), ath.get("shortName")) if n]


def _espn_competitions(ev: dict) -> list:
    """Un événement d'équipe porte UNE competition ; une carte MMA en porte
    une par combat. On les parcourt toutes."""
    return list(ev.get("competitions") or [])


def _espn_paire(comp: dict, home: str, away: str) -> tuple[dict, dict] | None:
    """(compétiteur domicile, compétiteur extérieur) si les deux noms du
    signal s'apparient strictement — par `homeAway` quand ESPN le donne,
    sinon (athlètes) dans les deux ordres ; None sinon."""
    comps = comp.get("competitors") or []
    dom, ext = _espn_camp(comp, "home"), _espn_camp(comp, "away")
    if dom and ext:
        paires = [(dom, ext)]
    elif len(comps) == 2:
        paires = [(comps[0], comps[1]), (comps[1], comps[0])]
    else:
        return None
    for a, b in paires:
        if (any(strict_team_match(home, n) for n in _espn_noms(a))
                and any(strict_team_match(away, n) for n in _espn_noms(b))):
            return a, b
    return None


def _espn_camp(competition: dict, camp: str) -> dict | None:
    for c in competition.get("competitors") or []:
        if c.get("homeAway") == camp:
            return c
    return None


def _espn_candidat(ev: dict, home: str, away: str) -> tuple[int, int] | None:
    """(home_score, away_score) si UNE competition de l'événement est TERMINÉE
    et que ses deux compétiteurs s'apparient strictement aux deux noms du
    signal ; None sinon. Sans score chiffré (combat MMA), le drapeau `winner`
    donne 1-0 / 0-1 — un combat sans vainqueur déclaré (no contest, nul)
    ne règle pas."""
    trouves = []
    for comp in _espn_competitions(ev):
        statut = (comp.get("status") or {}).get("type") or {}
        if not (statut.get("completed") and statut.get("state") == "post"):
            continue
        paire = _espn_paire(comp, home, away)
        if not paire:
            continue
        a, b = paire
        try:
            trouves.append((int(a.get("score")), int(b.get("score"))))
            continue
        except (TypeError, ValueError):
            pass
        if a.get("winner") is True and not b.get("winner"):
            trouves.append((1, 0))
        elif b.get("winner") is True and not a.get("winner"):
            trouves.append((0, 1))
    return trouves[0] if len(trouves) == 1 else None


def _espn_fenetre_entre(date_min: str, date_max: str) -> str | None:
    """`dates=AAAAMMJJ-AAAAMMJJ` couvrant [date_min − 1 j, date_max + 1 j]."""
    try:
        a = datetime.fromisoformat(date_min[:10]) - timedelta(days=1)
        b = datetime.fromisoformat(date_max[:10]) + timedelta(days=1)
    except (TypeError, ValueError):
        return None
    if b < a:
        a, b = b, a
    return f"{a:%Y%m%d}-{b:%Y%m%d}"


def fixtures_espn(sport: str, date_min: str, date_max: str) -> list | None:
    """Tous les événements ESPN (à venir, en cours, terminés) du sport sur la
    fenêtre — UNE requête par chemin. None si le sport n'a pas de chemin ESPN
    (aucune vérification possible), [] si ESPN n'a rien rendu."""
    chemins = _ESPN_PATHS.get((sport or "").lower())
    fenetre = _espn_fenetre_entre(date_min, date_max)
    if not chemins or not fenetre:
        return None
    out: list = []
    for path in chemins:
        out.extend(_espn_events(path, fenetre))
    return out


# Sports que règle api-sports sur son plan (core/api_sports.fetch_results :
# soccer, basketball, baseball, hockey). Repris ici pour que la liste des
# sports RÉGLABLES vive en un seul endroit, à côté des autres voies.
_API_SPORTS_SPORTS = frozenset({"soccer", "basketball", "baseball", "hockey"})


def sports_reglables() -> frozenset:
    """Les sports pour lesquels au moins UNE voie de règlement existe :
    api-sports, MLB statsapi (baseball) ou un chemin ESPN. Un sport absent
    d'ici — boxe, tennis — ne peut pas être réglé : le périmètre le refuse
    à l'émission (run_engine._reglable) et la politique de dépense ne paie
    plus ses cotes (core/scan_windows.SpendPolicy) — un crédit OddsAPI sur un
    match qu'on ne saura jamais régler est un crédit perdu deux fois."""
    return frozenset(_ESPN_PATHS) | _API_SPORTS_SPORTS | {"baseball"}


def fixture_connue(match_name: str, events: list) -> bool:
    """Le match (les DEUX noms, appariés strictement) figure-t-il dans ces
    événements ESPN, quel que soit leur état ? C'est le test de
    RÉGLABILITÉ : ce qu'ESPN liste avant le coup d'envoi, il le règle après."""
    parts = _split(match_name)
    if not parts:
        return False
    home, away = parts
    return any(_espn_paire(comp, home, away) is not None
               for ev in events for comp in _espn_competitions(ev))


def result_from_espn(match_name: str, sport: str, match_date: str) -> dict | None:
    """Score final via le scoreboard public ESPN — même contrat que les autres
    voies : les DEUX noms appariés strictement, candidat UNIQUE, statut
    terminé seulement, None sur panne."""
    parts = _split(match_name)
    if not parts:
        return None
    home, away = parts
    chemins = _ESPN_PATHS.get((sport or "").lower())
    fenetre = _espn_fenetre(match_date)
    if not chemins or not fenetre:
        return None
    vus: dict[str, tuple[int, int]] = {}
    for path in chemins:
        for ev in _espn_events(path, fenetre):
            score = _espn_candidat(ev, home, away)
            if score is not None:
                vus[str(ev.get("id") or f"{path}|{ev.get('date')}")] = score
    if len(vus) == 1:
        hs, as_ = next(iter(vus.values()))
        log.info("SETTLE espn | %s | %d-%d (0 appel IA)", match_name, hs, as_)
        return {"home_score": hs, "away_score": as_, "completed": True, "source": "espn"}
    if len(vus) > 1:
        log.info("SETTLE SKIP | %s — %d événements ESPN correspondent, on ne devine pas",
                 match_name, len(vus))
    return None


# ── 2. TheSportsDB ────────────────────────────────────────────────────

def _tsdb_key() -> str:
    return os.environ.get("THESPORTSDB_API_KEY") or _TSDB_PUBLIC_KEY


def _tsdb_teams(name: str, sport_label: str) -> list[str]:
    """idTeam candidats pour un nom d'équipe, filtrés par sport.

    ⚠️ GÉNÉRATEUR DE CANDIDATS SEULEMENT. searchteams est flou (« AD Pasto »
    rend « Pastoreo ») : rien de ce qui sort d'ici ne règle quoi que ce soit,
    seul l'appariement des DEUX noms sur l'événement décide.
    """
    ck = f"{sport_label}|{name.lower()}"
    if ck not in _CACHE_TSDB_TEAMS:
        q = urllib.parse.quote(name)
        data = _get_json(f"{TSDB_BASE}/{_tsdb_key()}/searchteams.php?t={q}",
                         _TSDB_BUCKET, TSDB_DAILY_BUDGET)
        ids = []
        for t in (data or {}).get("teams") or []:
            if sport_label and (t.get("strSport") or "") != sport_label:
                continue
            if t.get("idTeam"):
                ids.append(str(t["idTeam"]))
        _CACHE_TSDB_TEAMS[ck] = ids[:_TSDB_TEAMS_PER_SIDE]
    return _CACHE_TSDB_TEAMS[ck]


def _tsdb_last_events(team_id: str) -> list:
    if team_id not in _CACHE_TSDB_EVENTS:
        data = _get_json(f"{TSDB_BASE}/{_tsdb_key()}/eventslast.php?id={team_id}",
                         _TSDB_BUCKET, TSDB_DAILY_BUDGET)
        _CACHE_TSDB_EVENTS[team_id] = (data or {}).get("results") or []
    return _CACHE_TSDB_EVENTS[team_id]


def _tsdb_event_ok(ev: dict, home: str, away: str, jours: list[str]) -> bool:
    if ev.get("intHomeScore") is None or ev.get("intAwayScore") is None:
        return False
    statut = (ev.get("strStatus") or "").strip().lower()
    if statut not in _TSDB_FINISHED:
        return False                       # absent ou en direct : on attend
    if jours and (ev.get("dateEvent") or "") not in jours:
        return False
    return (strict_team_match(home, ev.get("strHomeTeam") or "")
            and strict_team_match(away, ev.get("strAwayTeam") or ""))


def result_from_thesportsdb(match_name: str, sport: str, match_date: str) -> dict | None:
    """Score final via TheSportsDB, résolu PAR ÉQUIPE (searchteams → eventslast).

    Sans date (relance d'une ligne de ledger orpheline), l'appariement des
    deux noms reste exigé et le candidat unique aussi : deux confrontations
    de la même paire dans les derniers résultats rendus → REFUS. (La clé
    gratuite ne rend qu'UN résultat par équipe — voir l'en-tête du module.)
    """
    parts = _split(match_name)
    if not parts:
        return None
    home, away = parts
    sport_label = _TSDB_SPORTS.get((sport or "").lower(), "")
    jours = _jours(match_date)

    vus: dict[str, dict] = {}
    for side_name in (home, away):
        for tid in _tsdb_teams(side_name, sport_label):
            for ev in _tsdb_last_events(tid):
                if not _tsdb_event_ok(ev, home, away, jours):
                    continue
                vus[str(ev.get("idEvent") or f"{tid}|{ev.get('dateEvent')}")] = ev
        if vus:
            break          # l'équipe domicile a suffi — pas de requêtes en plus

    if len(vus) == 1:
        ev = next(iter(vus.values()))
        try:
            hs, as_ = int(ev["intHomeScore"]), int(ev["intAwayScore"])
        except (TypeError, ValueError):
            return None
        log.info("SETTLE thesportsdb | %s | %d-%d | %s (0 appel IA)",
                 match_name, hs, as_, ev.get("strLeague") or "?")
        return {"home_score": hs, "away_score": as_,
                "completed": True, "source": "thesportsdb"}
    if len(vus) > 1:
        log.info("SETTLE SKIP | %s — %d événements TheSportsDB correspondent, "
                 "on ne devine pas", match_name, len(vus))
    return None


# ── Chaîne ────────────────────────────────────────────────────────────

def fetch_score(match_name: str, sport: str, match_date: str = "") -> dict | None:
    """Score final par la chaîne déterministe (après api-sports, étage 1 dans
    core/settlement.py). Rend {"home_score", "away_score", "completed": True,
    "source"} ou None — et None veut dire « pas trouvé AUJOURD'HUI », jamais
    un état terminal : la ligne repassera."""
    if (sport or "").lower() == "baseball":
        r = result_from_mlb(match_name, match_date)
        if r:
            return r
    r = result_from_espn(match_name, sport, match_date)
    if r:
        return r
    return result_from_thesportsdb(match_name, sport, match_date)
