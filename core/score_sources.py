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

DEUX SOURCES, APRÈS api-sports (qui reste l'étage 1, dans core/settlement.py) :

  1. MLB statsapi (statsapi.mlb.com) — officiel, sans clé, une requête par
     journée pour tout le slate. Ne sert que le baseball MLB.
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

from core import daily_quota
from core.paim_engine import strict_team_match

log = logging.getLogger("PREDATOR.score_sources")

MLB_URL = "https://statsapi.mlb.com/api/v1/schedule"
TSDB_BASE = "https://www.thesportsdb.com/api/v1/json"

# Clé publique documentée par TheSportsDB pour les tests/le gratuit. Un compte
# Patreon la remplace par THESPORTSDB_API_KEY (plus de requêtes, eventsday
# complet) sans toucher au code.
_TSDB_PUBLIC_KEY = "123"

# Budgets journaliers PRUDENTS côté PREDATOR (leçon api-sports, compte
# suspendu le 2026-08-20 : on bascule avant de se faire couper). statsapi ne
# publie pas de limite ; TheSportsDB gratuit tolère ~30 req/min.
MLB_DAILY_BUDGET = int(os.environ.get("MLB_STATSAPI_DAILY_BUDGET", "80"))
TSDB_DAILY_BUDGET = int(os.environ.get("THESPORTSDB_DAILY_BUDGET", "150"))
_MLB_BUCKET = "mlb_results"
_TSDB_BUCKET = "tsdb_results"

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
_CACHE_TSDB_TEAMS: dict[str, list] = {}
_CACHE_TSDB_EVENTS: dict[str, list] = {}


def reset_cache() -> None:
    """Vide les caches de run (tests, runs longs)."""
    _CACHE_MLB.clear()
    _CACHE_TSDB_TEAMS.clear()
    _CACHE_TSDB_EVENTS.clear()


def _get_json(url: str, bucket: str, budget: int) -> dict | None:
    """GET JSON avec budget partagé. None sur toute panne — jamais d'exception."""
    if daily_quota.spent(bucket) >= budget:
        log.warning("score_sources[%s]: budget journalier atteint (%d) — requête sautée",
                    bucket, budget)
        return None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "PREDATOR/1.0"})
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
    return result_from_thesportsdb(match_name, sport, match_date)
