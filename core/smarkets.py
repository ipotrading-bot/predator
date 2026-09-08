"""
core/smarkets.py — Smarkets Exchange : prix sharp gratuits, sans clé (2026-09-08).

POURQUOI CETTE SOURCE
---------------------
Le 2026-09-08 (soirée de Ligue des champions à 0 signal) l'opérateur a
demandé « le book gratuit le plus adapté ». Le gisement mesuré du pipeline
n'est pas un book soft de plus (odds-api.io ne délivre plus de clé gratuite,
constaté le même jour) mais la RÉFÉRENCE SHARP : chaque scan écarte ~7
matchs « MARCHÉ MORT » — un prix 1xbet/Bet365 sans exchange derrière — et
INCIDENTS.md (« Sources de cotes : lesquelles portent RÉELLEMENT un
signal ») désigne le second avis sharp indépendant comme le levier.
Matchbook est ce second avis ; Smarkets en est un troisième, sur les mêmes
ligues mineures où 1xbet est mou (sondé le 2026-09-08 17:40 UTC : 200
matchs de football à 30 h, Saudi Pro League, EFL Trophy, MLS Next Pro…).

CE QUE L'API DONNE (relevé le 2026-09-08, sans authentification)
----------------------------------------------------------------
  GET /v3/events/?state=upcoming&type=football_match&limit=200
      &end_datetime_max=…&sort=start_datetime,id  → 200 événements/page
  GET /v3/events/{id,id,…}/markets/?limit=200      → marchés, GROUPÉS par
      événement ; `market_type.name` : WINNER_3_WAY, OVER_UNDER (param =
      ligne), ASIAN_HANDICAP (param = ligne DOMICILE signée), WINNER_DNB…
      Les sous-marchés (FIRST_HALF_*, HOME_TEAM_*, CORNERS_*) portent leur
      propre type : le filtre par type suffit, pas de « 1st Half Total »
      déguisé comme chez Matchbook.
  GET /v3/markets/{id,…}/contracts/  et  /quotes/  → groupés par marché.
      Une cote est une PROBABILITÉ en centièmes de pour-cent : 4854 = 48,54 %
      → 2,06. `offers` = ce qu'on peut BACKER (le meilleur = le prix le plus
      BAS, donc la cote la plus haute), `bids` = ce qu'on peut LAYER (le
      meilleur = le prix le plus HAUT). Le milieu des deux est l'estimateur
      de probabilité vraie, comme chez Matchbook.
  Aucun en-tête de limite de débit ; 15 appels /markets/ en rafale → 200,
  mais des HTTP 429 sur les /quotes/ enchaînés (essai réel 2026-09-08
  17:45) : d'où `_PAUSE_S` entre deux appels et UNE reprise après 429.

BUDGET (règle 13)
-----------------
`SMARKETS_DAILY_BUDGET` = 2 000 requêtes/jour, tenu par core/daily_quota
(bucket « smarkets »), affiché à chaque scan : « N/2000 req aujourd'hui ».
Un scan coûte ~1 appel par sport + 1 par 25 événements + 2 par 60 marchés
utiles ≈ 100-130 appels (essai réel : 109 req, 137 marchés, 85 s avec les
reprises 429). Smarkets ne tourne QUE sur les scans standard (8/jour ≈
1 000 req), jamais en REPRICE : ce mode a 300 s de budget et Matchbook y
suffit. À budget atteint la source rend {} en le disant — jamais une
exception.

CRITÈRE DE RETRAIT (règle 13) — à relever le 2026-09-22
--------------------------------------------------------
Mesure : la ligne de log « Smarkets OK — N marchés sharp (+M nouveaux hors
Matchbook/Betfair, total exchange T) » des scans standard du 09 au 21/09 —
M est compté par le rapprochement flou du projet (core.exchange_match),
pas par clé brute : « Sheff Utd » et « Sheffield United » sont un match.
Smarkets SORT si l'une des deux conditions est vraie :
  · M (marchés qu'aucun autre exchange ne couvrait) < 10 % de T en médiane
    sur ces scans — elle ne ferait que recopier Matchbook ;
  · HTTP 401/403/451 (géoblocage) sur 3 scans standard consécutifs.
Le jour du retrait : ligne dans INCIDENTS.md, suppression du registre
(`core.source_adapter.CALL_ORDER`) — `tests/test_smarkets.py` tombe si l'un
bouge sans l'autre.

Sortie : {norm_key: {"match","home","away","1","X","2","commence_time",
"_source":"smarkets", "totals"?, "spreads"?}} — la forme exacte de
core/matchbook.fetch_matchbook_prices, pour le même chemin d'enrichissement
et le même pool de closing line dans run_engine. Rend {} sur toute panne.
"""
import logging
import os
import time
from datetime import datetime, timedelta, timezone

import requests

from core import daily_quota
from core.matchbook import (MAX_OVERROUND, MAX_SPREAD_RATIO, MIN_OVERROUND, _echelle,
                            _sans_prive, _split_teams, norm_key)
from core.paim_engine import strict_team_match

log = logging.getLogger("PREDATOR.smarkets")

BASE_URL = "https://api.smarkets.com/v3"
TIMEOUT = int(os.environ.get("SMARKETS_TIMEOUT", "15"))
_HEADERS = {"Accept": "application/json", "User-Agent": "Mozilla/5.0"}

QUOTA_BUCKET = "smarkets"
DAILY_BUDGET = int(os.environ.get("SMARKETS_DAILY_BUDGET", "2000"))
# Débit : pause entre deux appels, et une reprise après un 429 (mesuré le
# 2026-09-08 : les /quotes/ groupés enchaînés sans pause prennent des 429).
_PAUSE_S = float(os.environ.get("SMARKETS_PAUSE_S", "1.0"))
_RETRY_429_S = float(os.environ.get("SMARKETS_RETRY_429_S", "3.0"))

# Types d'événement Smarkets par sport du moteur (relevé le 2026-09-08 :
# basketball_match existe mais était vide hors saison).
EVENT_TYPES = {
    "soccer":           "football_match",
    "tennis":           "tennis_match",
    "hockey":           "ice_hockey_match",
    "baseball":         "baseball_match",
    "basketball":       "basketball_match",
    "mma":              "mma_match",
    "americanfootball": "american_football_match",
}
# Événements lus par sport et par scan (les plus proches d'abord) : borne le
# nombre d'appels marchés/contrats/cotes, donc le budget.
MAX_EVENTS = int(os.environ.get("SMARKETS_MAX_EVENTS", "150"))
EVENTS_PER_MARKETS_CALL = 25
MARKETS_PER_CALL = 60

_WINNER_TYPES = ("WINNER_3_WAY", "WINNER_2_WAY", "WINNER", "MONEY_LINE", "MATCH_ODDS")
_TOTALS_TYPES = ("OVER_UNDER",)
_HANDICAP_TYPES = ("ASIAN_HANDICAP",)
_USEFUL = set(_WINNER_TYPES) | set(_TOTALS_TYPES) | set(_HANDICAP_TYPES)


# ── Cotes ────────────────────────────────────────────────────────────────

def _odds(price) -> float | None:
    """Prix Smarkets (centièmes de %) → cote décimale. None hors bornes."""
    try:
        p = float(price)
    except (TypeError, ValueError):
        return None
    if p <= 0 or p >= 10000:
        return None
    return 10000.0 / p


def mid_from_quote(quote: dict | None) -> float | None:
    """Milieu back/lay d'un contrat, ou None si le carnet est vide, croisé ou
    trop large (MAX_SPREAD_RATIO, la même borne que Matchbook).

    back = cote la plus HAUTE qu'on peut prendre = prix `offers` le plus BAS ;
    lay = cote la plus BASSE à laquelle on peut vendre = prix `bids` le plus
    HAUT. Un carnet vide d'un côté n'a pas de milieu : on ne fabrique pas de
    prix sur une seule jambe."""
    if not quote:
        return None
    offers = [p for p in (_odds(o.get("price")) for o in quote.get("offers") or []) if p]
    bids = [p for p in (_odds(b.get("price")) for b in quote.get("bids") or []) if p]
    if not offers or not bids:
        return None
    back, lay = max(offers), min(bids)
    if lay < back or back <= 1.01:
        return None
    if lay / back > MAX_SPREAD_RATIO:
        return None
    return round((back + lay) / 2, 4)


# ── Marchés ──────────────────────────────────────────────────────────────

def _side(name: str, home: str, away: str) -> str | None:
    """« 1 » / « X » / « 2 » depuis le nom d'un contrat 1X2, avec le repli
    flou du projet, refusé dès que les DEUX équipes matchent."""
    low = name.strip().lower()
    if low == "draw":
        return "X"
    if low == home.lower():
        return "1"
    if low == away.lower():
        return "2"
    h, a = strict_team_match(name, home), strict_team_match(name, away)
    if h and not a:
        return "1"
    if a and not h:
        return "2"
    return None


def winner_odds(contracts: list, quotes: dict, home: str, away: str) -> dict | None:
    odds = {"1": 0.0, "X": 0.0, "2": 0.0}
    for c in contracts:
        mid = mid_from_quote(quotes.get(str(c.get("id"))))
        if mid is None:
            continue
        side = _side(str(c.get("name", "")), home, away)
        if side:
            odds[side] = mid
    if odds["1"] <= 1.01 or odds["2"] <= 1.01:
        return None
    book = 1 / odds["1"] + 1 / odds["2"] + (1 / odds["X"] if odds["X"] > 1.01 else 0.0)
    if not (MIN_OVERROUND <= book <= MAX_OVERROUND):
        log.debug("Smarkets: %s vs %s écarté — somme des probas %.3f", home, away, book)
        return None
    return odds


def _param(market: dict) -> float | None:
    try:
        return float((market.get("market_type") or {}).get("param"))
    except (TypeError, ValueError):
        return None


def totals_row(market: dict, contracts: list, quotes: dict) -> dict | None:
    point = _param(market)
    if point is None:
        return None
    over = under = None
    for c in contracts:
        mid = mid_from_quote(quotes.get(str(c.get("id"))))
        if mid is None:
            continue
        name = str(c.get("name", "")).strip().upper()
        if name.startswith("OVER"):
            over = mid
        elif name.startswith("UNDER"):
            under = mid
    if over and under:
        return {"over": over, "under": under, "point": abs(point), "_a": over, "_b": under}
    return None


def handicap_row(market: dict, contracts: list, quotes: dict, home: str, away: str) -> dict | None:
    """`param` est la ligne DOMICILE signée (« Asian Handicap A -1.5 / B +1.5 »
    → -1.5). Les contrats se nomment « A -1.5 » / « B +1.5 » ; l'équipe se
    lit sur le nom, la ligne sur `param`, comme le moteur l'attend
    (`point` = domicile, `away_point` = -point)."""
    point = _param(market)
    if point is None:
        return None
    row: dict = {}
    for c in contracts:
        mid = mid_from_quote(quotes.get(str(c.get("id"))))
        if mid is None:
            continue
        name = str(c.get("name", "")).strip()
        bare = name.rsplit(" ", 1)[0].strip() if name and name[-1].isdigit() else name
        if bare.lower() == home.lower() or (
                strict_team_match(bare, home) and not strict_team_match(bare, away)):
            row["home"] = mid
        elif bare.lower() == away.lower() or (
                strict_team_match(bare, away) and not strict_team_match(bare, home)):
            row["away"] = mid
    if row.get("home") and row.get("away"):
        return {**row, "point": point, "away_point": -point, "_a": row["home"], "_b": row["away"]}
    return None


# ── Réseau ───────────────────────────────────────────────────────────────

class _Geoblocked(Exception):
    pass


def _get(path: str, params: dict | None = None):
    """Un appel (deux au plus, sur 429), compté dans le budget. None sur
    panne ; lève _Geoblocked sur 401/403/451 (le cas redouté des runners US)."""
    r = None
    for tentative in (1, 2):
        daily_quota.add(QUOTA_BUCKET, 1)
        if _PAUSE_S:
            time.sleep(_PAUSE_S)
        try:
            r = requests.get(f"{BASE_URL}{path}", params=params, headers=_HEADERS, timeout=TIMEOUT)
        except Exception as e:
            log.warning("Smarkets injoignable (%s)", e)
            return None
        if r.status_code != 429 or tentative == 2:
            break
        log.info("Smarkets: HTTP 429 — reprise dans %.0fs", _RETRY_429_S)
        time.sleep(_RETRY_429_S)
    if r.status_code in (401, 403, 451):
        raise _Geoblocked(f"HTTP {r.status_code}")
    if r.status_code != 200:
        log.warning("Smarkets: HTTP %d sur %s", r.status_code, path.split("/", 3)[1] if "/" in path else path)
        return None
    try:
        return r.json() or {}
    except Exception as e:
        log.warning("Smarkets: réponse illisible (%s)", e)
        return None


def _chunks(items: list, n: int):
    for i in range(0, len(items), n):
        yield items[i:i + n]


def _events(sport: str, now: datetime, until: datetime) -> list[dict]:
    body = _get("/events/", {
        "state": "upcoming", "type": EVENT_TYPES[sport],
        "start_datetime_min": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end_datetime_max": until.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sort": "start_datetime,id", "limit": str(min(MAX_EVENTS, 200)),
    })
    return list((body or {}).get("events") or [])


def fetch_smarkets_prices(sports: list | None = None, hours_ahead: int = 24) -> dict:
    """Prix sharp Smarkets pour les matchs à venir — voir l'en-tête du module."""
    wanted = [s for s in (sports or ["soccer"]) if s in EVENT_TYPES]
    if not wanted:
        return {}
    if daily_quota.spent(QUOTA_BUCKET) >= DAILY_BUDGET:
        log.warning("Smarkets: budget journalier atteint (%d/%d) — source sautée",
                    daily_quota.spent(QUOTA_BUCKET), DAILY_BUDGET)
        return {}
    now = datetime.now(timezone.utc)
    until = now + timedelta(hours=hours_ahead)
    out: dict = {}
    n_events = n_totals = n_spreads = skipped_thin = 0
    try:
        for sport in wanted:
            events = _events(sport, now, until)
            by_id: dict[str, dict] = {}
            for ev in events:
                teams = _split_teams(str(ev.get("name", "")))
                start = str(ev.get("start_datetime", ""))
                try:
                    when = datetime.fromisoformat(start.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if not teams or when < now or when > until:
                    continue
                by_id[str(ev.get("id"))] = {"home": teams[0], "away": teams[1], "when": when}
            n_events += len(by_id)
            if not by_id:
                continue
            # Marchés utiles, groupés par événement
            markets: dict[str, dict] = {}
            for ids in _chunks(list(by_id), EVENTS_PER_MARKETS_CALL):
                if daily_quota.spent(QUOTA_BUCKET) >= DAILY_BUDGET:
                    break
                body = _get(f"/events/{','.join(ids)}/markets/", {"limit": "500"})
                for m in (body or {}).get("markets") or []:
                    mtype = str((m.get("market_type") or {}).get("name", ""))
                    if mtype in _USEFUL and str(m.get("state", "open")) == "open":
                        markets[str(m.get("id"))] = m
            # Contrats et cotes, groupés par marché
            contracts: dict[str, list] = {}
            quotes: dict[str, dict] = {}
            for ids in _chunks(list(markets), MARKETS_PER_CALL):
                if daily_quota.spent(QUOTA_BUCKET) >= DAILY_BUDGET:
                    break
                body = _get(f"/markets/{','.join(ids)}/contracts/", {"limit": "1000"})
                for c in (body or {}).get("contracts") or []:
                    contracts.setdefault(str(c.get("market_id")), []).append(c)
                body = _get(f"/markets/{','.join(ids)}/quotes/")
                for cid, q in (body or {}).items():
                    if isinstance(q, dict):
                        quotes[str(cid)] = q
            # Assemblage par événement
            per_event: dict[str, dict] = {}
            for mid, m in markets.items():
                per_event.setdefault(str(m.get("event_id")), {"winner": None, "totals": [], "spreads": []})
                slot = per_event[str(m.get("event_id"))]
                mtype = str((m.get("market_type") or {}).get("name", ""))
                ev = by_id.get(str(m.get("event_id")))
                if not ev:
                    continue
                cs = contracts.get(mid) or []
                if mtype in _WINNER_TYPES and slot["winner"] is None:
                    slot["winner"] = winner_odds(cs, quotes, ev["home"], ev["away"])
                elif mtype in _TOTALS_TYPES:
                    row = totals_row(m, cs, quotes)
                    if row:
                        slot["totals"].append(row)
                elif mtype in _HANDICAP_TYPES:
                    row = handicap_row(m, cs, quotes, ev["home"], ev["away"])
                    if row:
                        slot["spreads"].append(row)
            for eid, slot in per_event.items():
                ev = by_id[eid]
                if not slot["winner"]:
                    skipped_thin += 1
                    continue
                row = {
                    "match": f"{ev['home']} vs {ev['away']}", "home": ev["home"], "away": ev["away"],
                    "1": slot["winner"]["1"], "X": slot["winner"]["X"], "2": slot["winner"]["2"],
                    "commence_time": ev["when"].strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "_source": "smarkets",
                }
                if slot["totals"]:
                    ech = [_sans_prive(c) for c in _echelle(slot["totals"])]
                    row["totals"] = {**ech[0], "ladder": ech}
                    n_totals += 1
                if slot["spreads"]:
                    ech = [_sans_prive(c) for c in _echelle(slot["spreads"])]
                    row["spreads"] = {**ech[0], "ladder": ech}
                    n_spreads += 1
                out[norm_key(ev["home"], ev["away"])] = row
    except _Geoblocked as e:
        log.warning("Smarkets: %s — probable géoblocage depuis cette IP (runners GitHub "
                    "en zone US) ; critère de retrait règle 13 : 3 scans consécutifs", e)
        return {}
    log.info("Smarkets: %d marchés sharp — dont %d totals, %d handicaps "
             "(%d événements | %d trop peu liquides) | %d/%d req aujourd'hui",
             len(out), n_totals, n_spreads, n_events, skipped_thin,
             daily_quota.spent(QUOTA_BUCKET), DAILY_BUDGET)
    return out


def probe() -> tuple[bool, str]:
    """(joignable ?, détail) — pour scripts/ops.py sources. Un seul appel."""
    try:
        r = requests.get(f"{BASE_URL}/events/", headers=_HEADERS, timeout=TIMEOUT,
                         params={"state": "upcoming", "type": EVENT_TYPES["soccer"], "limit": "1"})
    except Exception as e:
        return False, f"injoignable ({type(e).__name__})"
    if r.status_code != 200:
        geo = " — probable géoblocage" if r.status_code in (401, 403, 451) else ""
        return False, f"HTTP {r.status_code}{geo}"
    return True, f"HTTP 200 — {daily_quota.spent(QUOTA_BUCKET)}/{DAILY_BUDGET} req aujourd'hui"
