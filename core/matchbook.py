"""
core/matchbook.py — Matchbook Exchange : prix SHARP, sans clé, sans compte.

CE QUE ÇA RÉSOUT
----------------
Le pipeline sait trouver des prix SOFT (api-sports, LineFeed) mais le côté
SHARP — celui qui donne la probabilité vraie par dévigorisation — ne venait
que d'OddsAPI/Pinnacle, de Betfair, ou d'une estimation par IA. Résultat :
OddsAPI à sec = plus aucun signal, quelle que soit l'abondance des prix soft
(incident du 10→20 août 2026, dix jours à zéro).

Matchbook est un EXCHANGE : le back et le lay encadrent la probabilité vraie,
et le milieu des deux donne une marge d'environ 0,1 % — meilleur que Pinnacle
(~2 %). C'est donc une référence sharp de premier ordre, et son endpoint
`/edge/rest/events` répond sans en-tête d'authentification.

POURQUOI PAS BETFAIR (déjà intégré)
-----------------------------------
Betfair exige une App Key « Live » à 499 £ et refuse les IP américaines
(`BETTING_RESTRICTED_LOCATION`, géo MaxMind) : les runners GitHub sont en
zone interdite, l'intégration existante ne peut structurellement pas
aboutir. Matchbook n'a ni clé ni coût.

⚠️ LE POINT NON VÉRIFIÉ, À SURVEILLER DANS LES LOGS
---------------------------------------------------
Testé OK le 2026-08-20 (HTTP 200, 278 matchs de foot) — mais depuis une IP
Azure géolocalisée **GB**, pas depuis un runner GitHub **US**. Les exchanges
géobloquent couramment les États-Unis ; il est donc possible que cette
source réponde 403/451 en production là où elle répond 200 en test. Le code
traite ce cas comme une absence de données (log explicite, jamais
d'exception), et `scripts/ops.py sources` le sonde. Chercher
« Matchbook » dans les logs Actions pour trancher.

CGU
---
La Fair Usage Policy de Matchbook interdit de revendre/redistribuer les
données à un tiers. Usage interne de calcul = conforme ; ne pas republier
ces cotes telles quelles sur le dashboard public. C'est pourquoi ce module
alimente le MOTEUR (référence de prix) et non l'affichage.
"""
import logging
import os
from datetime import datetime, timedelta, timezone

import requests

from core.paim_engine import strict_team_match

log = logging.getLogger("PREDATOR.matchbook")

BASE_URL = "https://api.matchbook.com/edge/rest"

# Nom de sport interne PREDATOR -> sport-id Matchbook (relevé sur
# /lookups/sports le 2026-08-20).
SPORT_IDS = {
    "soccer":           15,
    "basketball":        4,
    "baseball":          3,
    "hockey":            6,
    "americanfootball":  1,
    "tennis":            9,
    "mma":             126,
    "cricket":         110,
    "darts":           116,
    "rugbyleague":     114,
    "rugby":            18,
    "aussierules":     112,
}

# Un marché d'exchange peu liquide affiche des cotes fantaisistes (back 110
# contre lay 1.01). Deux garde-fous, tous deux nécessaires :
#   - chaque issue doit avoir un back ET un lay (sinon aucune fourchette) ;
#   - la fourchette lay/back doit rester serrée.
MAX_SPREAD_RATIO = float(os.environ.get("MATCHBOOK_MAX_SPREAD", "1.30"))
# Après passage au milieu de fourchette, la somme des probabilités doit
# tourner autour de 1. Hors de cette plage, le carnet est périmé ou trop
# mince : on jette plutôt que d'injecter une référence fausse dans le devig.
MIN_OVERROUND = float(os.environ.get("MATCHBOOK_MIN_OVERROUND", "0.90"))
MAX_OVERROUND = float(os.environ.get("MATCHBOOK_MAX_OVERROUND", "1.12"))

PER_PAGE  = int(os.environ.get("MATCHBOOK_PER_PAGE", "100"))
# Le quota est de 700 requêtes/minute sur ce groupe d'endpoints : paginer
# largement ne coûte rien. Sans pagination on ne voyait que les 60 premiers
# matchs alors que le seul foot en compte ~280 dans la fenêtre.
MAX_PAGES = int(os.environ.get("MATCHBOOK_MAX_PAGES", "5"))
TIMEOUT  = int(os.environ.get("MATCHBOOK_TIMEOUT", "20"))

_HEADERS = {"Accept": "application/json", "User-Agent": "Mozilla/5.0"}

# Marché « vainqueur du match » selon les sports.
_WINNER_MARKETS = ("one_x_two", "match_odds", "money_line", "moneyline")


def norm_key(home: str, away: str) -> str:
    """Clé de rapprochement — identique à celle de fetch_betfair_prices()."""
    return f"{home.lower().strip()}_{away.lower().strip()}"


def _best(prices: list, side: str) -> float | None:
    """Meilleure cote disponible d'un côté : la plus HAUTE en back (ce que
    touche le parieur), la plus BASSE en lay (ce qu'il en coûte)."""
    vals = []
    for p in prices or []:
        if str(p.get("side", "")).lower() != side:
            continue
        try:
            odd = float(p.get("odds"))
        except (TypeError, ValueError):
            continue
        if odd > 1.01:
            vals.append(odd)
    if not vals:
        return None
    return max(vals) if side == "back" else min(vals)


def _mid(prices: list) -> float | None:
    """Milieu back/lay — l'estimateur de probabilité vraie de l'exchange.
    None si la fourchette est absente ou trop large pour être crédible."""
    back = _best(prices, "back")
    lay = _best(prices, "lay")
    if back is None or lay is None:
        return None
    if lay < back:                     # carnet croisé : incohérent, on jette
        return None
    if lay / back > MAX_SPREAD_RATIO:
        return None
    return round((back + lay) / 2, 4)


def _split_teams(name: str) -> tuple[str, str] | None:
    """« A vs B » → (A, B) ; « A at B » → (B, A).

    Le « at » américain inverse l'ordre : « Raiders at Texans » signifie que
    Houston reçoit. Confondre les deux intervertirait domicile et extérieur,
    donc les cotes 1 et 2 — un signal parfaitement inversé, et silencieux.
    """
    for sep, swap in ((" vs ", False), (" v ", False), (" at ", True), (" @ ", True)):
        if sep in name:
            a, b = name.split(sep, 1)
            a, b = a.strip(), b.strip()
            if not a or not b:
                return None
            return (b, a) if swap else (a, b)
    return None


def _match_odds(event: dict, home: str, away: str) -> dict | None:
    """{"1","X","2"} depuis le marché one_x_two, en milieu de fourchette."""
    for market in event.get("markets") or []:
        # Le foot expose « one_x_two » (avec nul), les sports US/tennis
        # exposent « money_line » (deux issues). Ne filtrer que sur le
        # premier écartait silencieusement basket, baseball, hockey et
        # tennis — vérifié le 2026-08-20.
        if str(market.get("market-type", "")).lower() not in _WINNER_MARKETS:
            continue
        if str(market.get("status", "")).lower() not in ("open", ""):
            continue
        odds = {"1": 0.0, "X": 0.0, "2": 0.0}
        for runner in market.get("runners") or []:
            rname = str(runner.get("name", "")).strip()
            mid = _mid(runner.get("prices"))
            if mid is None:
                continue
            low = rname.lower()
            if low == "draw":
                odds["X"] = mid
            elif low == home.lower():
                odds["1"] = mid
            elif low == away.lower():
                odds["2"] = mid
            # Repli sur le rapprochement flou du projet : l'exchange abrège
            # (« Man Utd » contre « Manchester United »). On n'accepte le
            # flou que si l'AUTRE équipe ne matche pas aussi — sinon on
            # risquerait d'intervertir 1 et 2, l'erreur la plus coûteuse.
            elif strict_team_match(rname, home) and not strict_team_match(rname, away):
                odds["1"] = mid
            elif strict_team_match(rname, away) and not strict_team_match(rname, home):
                odds["2"] = mid
        if odds["1"] > 1.01 and odds["2"] > 1.01:
            book = 1 / odds["1"] + 1 / odds["2"]
            if odds["X"] > 1.01:
                book += 1 / odds["X"]
            if MIN_OVERROUND <= book <= MAX_OVERROUND:
                return odds
            log.debug("Matchbook: %s vs %s écarté — somme des probas %.3f", home, away, book)
        return None
    return None


def _line_point(market: dict, runner_name: str) -> float | None:
    """Le nombre signé de la ligne : « Club Tacuary +1.5 » → +1.5.

    On le lit sur le RUNNER et non sur le champ `handicap` du marché, qui
    est non signé : sans le signe, un handicap +1.5 et un -1.5 deviendraient
    indistinguables et le moteur comparerait deux lignes opposées.
    """
    for token in reversed(str(runner_name).replace("−", "-").split()):
        try:
            return float(token.replace("+", ""))
        except ValueError:
            continue
    try:
        return float(market.get("handicap"))
    except (TypeError, ValueError):
        return None


def _echelle(candidates: list) -> list[dict]:
    """Toutes les lignes cotées, la plus équilibrée en tête.

    Un exchange cote une douzaine de lignes ; les extrêmes (1.01 contre 8.60)
    sont sans rapport avec la ligne de référence du marché — d'où le tri par
    écart de prix, la même heuristique que `core/odds_api_io.echelle`.

    Mais « la même heuristique des deux côtés » ne donne PAS la même ligne :
    chaque source l'applique à SON carnet, et le book soft n'a aucune raison
    d'équilibrer sa cote sur le même handicap que l'exchange. Les deux
    retenaient donc des lignes différentes, et `run_engine._meme_ligne`
    refusait la paire — à raison, mais pour rien : la ligne du sharp était
    cotée chez le soft aussi, on venait de la jeter. On garde donc l'échelle
    entière, et `run_engine._aligner_sur_meme_ligne` y retrouve la ligne
    commune. La ligne principale reste `_echelle(...)[0]`, inchangée.
    """
    return sorted(candidates, key=lambda c: abs(c["_a"] - c["_b"]))


def _sans_prive(row: dict) -> dict:
    """La ligne, débarrassée des champs de travail `_a`/`_b`."""
    return {k: v for k, v in row.items() if not k.startswith("_")}


# Qualificatifs d'un SOUS-marché : même `market-type` (« total », « handicap »),
# mais pas le même pari. Relevé sur l'API le 2026-08-28 : un match de football
# porte QUATRE marchés de type « total » — « Total », « 1st Half Total »,
# « Home Team Total Goals », « Away Team Total Goals » — et les runners
# s'appellent tous « OVER 2.5 » / « UNDER 2.5 ».
_SOUS_MARCHE = ("half", "1st", "2nd", "team", "quarter", "period",
                "corner", "card", "booking")


def _est_sous_marche(market: dict) -> bool:
    """Un marché de mi-temps, par équipe, de corners… n'est PAS le marché
    du match entier, même si son type et ses runners sont identiques.

    Le 2026-08-28 15:58, « Al-Riyadh SC vs Neom SC | SOC Under 2.5 —
    EV 80.84 % » (refusé par MAX_EDGE, donc sans dégât) : l'échelle des
    totals contenait le point 2.5 DEUX fois — « Total » (under 2.68) et
    « 1st Half Total » (under 1.21). `run_engine._aligner_sur_meme_ligne`
    indexe l'échelle par point, le dernier gagne : le moteur comparait le
    Under 2.5 du match entier chez le soft au Under 2.5 de la première
    mi-temps chez l'exchange. Un edge à 80 % sur une ligne « alignée » —
    exactement l'artefact d'A6 (deux paris différents comparés), par une
    autre porte.
    """
    name = str(market.get("name", "")).lower()
    return any(q in name for q in _SOUS_MARCHE)


def _totals_odds(event: dict) -> dict | None:
    """{"over","under","point"} — forme attendue par run_engine._process_totals.
    Seul le total du MATCH ENTIER : voir `_est_sous_marche`."""
    cands = []
    for market in event.get("markets") or []:
        if str(market.get("market-type", "")).lower() != "total":
            continue
        if str(market.get("status", "")).lower() not in ("open", ""):
            continue
        if _est_sous_marche(market):
            continue
        over = under = None
        point = None
        for runner in market.get("runners") or []:
            name = str(runner.get("name", "")).strip().upper()
            mid = _mid(runner.get("prices"))
            if mid is None:
                continue
            if name.startswith("OVER"):
                over, point = mid, _line_point(market, name)
            elif name.startswith("UNDER"):
                under = mid
        if over and under and point is not None:
            cands.append({"over": over, "under": under, "point": abs(point),
                          "_a": over, "_b": under})
    ech = [_sans_prive(c) for c in _echelle(cands)]
    return {**ech[0], "ladder": ech} if ech else None


def _handicap_odds(event: dict, home: str, away: str) -> dict | None:
    """{"home","away","point","away_point"} — forme de _process_spreads."""
    cands = []
    for market in event.get("markets") or []:
        if str(market.get("market-type", "")).lower() not in ("handicap", "asian_handicap"):
            continue
        if str(market.get("status", "")).lower() not in ("open", ""):
            continue
        if _est_sous_marche(market):
            continue                  # même garde que les totals, même raison
        row: dict = {}
        for runner in market.get("runners") or []:
            rname = str(runner.get("name", "")).strip()
            mid = _mid(runner.get("prices"))
            if mid is None:
                continue
            point = _line_point(market, rname)
            # Le nom du runner porte l'équipe ET sa ligne ; on retire la
            # ligne avant de rapprocher, sinon « Tacuary +1.5 » ne matche
            # jamais « Tacuary ».
            bare = rname.rsplit(" ", 1)[0].strip() if point is not None else rname
            if bare.lower() == home.lower() or (
                    strict_team_match(bare, home) and not strict_team_match(bare, away)):
                row["home"], row["point"] = mid, point
            elif bare.lower() == away.lower() or (
                    strict_team_match(bare, away) and not strict_team_match(bare, home)):
                row["away"], row["away_point"] = mid, point
        if row.get("home") and row.get("away") and row.get("point") is not None:
            row.setdefault("away_point", -row["point"])
            row["_a"], row["_b"] = row["home"], row["away"]
            cands.append(row)
    ech = [_sans_prive(c) for c in _echelle(cands)]
    return {**ech[0], "ladder": ech} if ech else None


def fetch_matchbook_prices(sports: list | None = None, hours_ahead: int = 24) -> dict:
    """
    Prix sharp d'exchange pour les matchs à venir.

    Returns {norm_key: {"match","home","away","1","X","2","_source"}} —
    même forme que core/harvester.py::fetch_betfair_prices(), pour se
    brancher sur le même chemin d'enrichissement dans run_engine.py.
    Rend {} sur toute panne : cette source est un bonus, jamais un point
    de défaillance.
    """
    wanted = sports or ["soccer", "basketball", "baseball", "hockey"]
    ids = [SPORT_IDS[s] for s in wanted if s in SPORT_IDS]
    if not ids:
        return {}

    now = datetime.now(timezone.utc)
    until = now + timedelta(hours=hours_ahead)
    base_params = {
        "sport-ids": ",".join(str(i) for i in ids),
        "include-prices": "true",
        "exchange-type": "back-lay",
        "odds-type": "DECIMAL",
        "price-depth": "3",
        "per-page": str(PER_PAGE),
        "after": str(int(now.timestamp())),
        "before": str(int(until.timestamp())),
        "states": "open",
    }
    events: list = []
    for page in range(MAX_PAGES):
        params = dict(base_params, offset=str(page * PER_PAGE))
        try:
            r = requests.get(f"{BASE_URL}/events", params=params,
                             headers=_HEADERS, timeout=TIMEOUT)
        except Exception as e:
            log.warning("Matchbook injoignable (%s) — source ignorée", e)
            break
        if r.status_code in (401, 403, 451):
            # Le cas redouté : géoblocage depuis un runner US (Betfair fait
            # pareil). À constater ici plutôt qu'à deviner.
            log.warning("Matchbook: HTTP %d — probable géoblocage depuis cette IP "
                        "(les runners GitHub sont en zone US)", r.status_code)
            return {}
        if r.status_code != 200:
            log.warning("Matchbook: HTTP %d (page %d)", r.status_code, page + 1)
            break
        try:
            body = r.json() or {}
        except Exception as e:
            log.warning("Matchbook: réponse illisible (%s)", e)
            break
        batch = body.get("events") or []
        events.extend(batch)
        try:
            total = int(body.get("total", 0))
        except (TypeError, ValueError):
            total = 0
        if len(batch) < PER_PAGE or len(events) >= total:
            break

    out: dict = {}
    skipped_live = skipped_thin = 0
    n_totals = n_spreads = 0
    for ev in events:
        try:
            if ev.get("in-running-flag"):
                skipped_live += 1
                continue          # PREDATOR ne joue que du pré-match
            teams = _split_teams(str(ev.get("name", "")))
            if not teams:
                continue
            home, away = teams
            start = str(ev.get("start", ""))
            try:
                when = datetime.fromisoformat(start.replace("Z", "+00:00"))
            except ValueError:
                continue
            if when < now or when > until:
                continue
            odds = _match_odds(ev, home, away)
            if not odds:
                skipped_thin += 1
                continue
            row = {
                "match": f"{home} vs {away}", "home": home, "away": away,
                "1": odds["1"], "X": odds["X"], "2": odds["2"],
                "commence_time": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "_source": "matchbook",
            }
            # Totals et handicaps : l'exchange en publie ~15x plus que de
            # 1X2 (574 handicaps / 468 totals contre 34 marchés 1X2 relevés
            # le 2026-08-20). Les ignorer laissait l'essentiel de la source
            # inexploité, alors que le moteur sait traiter ces marchés dès
            # qu'il a les deux côtés.
            tot = _totals_odds(ev)
            if tot:
                row["totals"] = tot
                n_totals += 1
            hcp = _handicap_odds(ev, home, away)
            if hcp:
                row["spreads"] = hcp
                n_spreads += 1
            out[norm_key(home, away)] = row
        except Exception as e:
            log.debug("Matchbook parse: %s", e)

    log.info("Matchbook: %d marchés sharp — dont %d totals, %d handicaps "
             "(%d bruts | %d en direct | %d trop peu liquides)",
             len(out), n_totals, n_spreads, len(events), skipped_live, skipped_thin)
    return out


def probe() -> tuple[bool, str]:
    """(joignable ?, détail) — pour scripts/ops.py sources. Ne consomme rien."""
    try:
        r = requests.get(f"{BASE_URL}/events", headers=_HEADERS, timeout=TIMEOUT,
                         params={"sport-ids": SPORT_IDS["soccer"], "per-page": "1",
                                 "states": "open"})
    except Exception as e:
        return False, f"injoignable ({type(e).__name__})"
    if r.status_code != 200:
        geo = " — probable géoblocage" if r.status_code in (401, 403, 451) else ""
        return False, f"HTTP {r.status_code}{geo}"
    try:
        total = (r.json() or {}).get("total", "?")
    except Exception:
        total = "?"
    return True, f"HTTP 200 — {total} matchs de foot ouverts"
