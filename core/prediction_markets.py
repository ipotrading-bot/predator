"""
core/prediction_markets.py — Kalshi & Polymarket : rôle `consensus`, jamais sharp.

CE QUE ÇA APPORTE
-----------------
Un TROISIÈME avis, indépendant de tout bookmaker. Quand OddsAPI et 500.com
donnent tous deux un prix « Pinnacle », rien ne dit lequel est périmé — ils
peuvent même partager la panne (500.com recopie les books, il ne les
interroge pas). Un marché de prédiction ne recopie personne : c'est de
l'argent réel posé par des gens qui n'ont pas lu la même ligne. C'est ce qui
en fait un arbitre utile dans `source_adapter.cross_check`.

PREUVE DE VIE ET DE VALEUR (2026-08-22, IP datacenter)
-------------------------------------------------------
Sur Hull City–Manchester United (英超, la même rencontre que 500.com publie
sous « 赫尔城 vs 曼联 »), Polymarket cotait 0,095 / 0,185 / 0,715. Confronté
aux deux chemins sharp de 500.com, après dévigorisation :

    écart en POINTS de probabilité        1        X        2
    500/Pinnacle vs Polymarket          0,93     0,14     1,07
    500/Betfair  vs Polymarket          0,41     0,16     0,25

Trois hôtes sans aucun lien s'accordent à ~1 point. C'est cette mesure qui a
validé la carte des books masqués de 500.com (core/odds500.py) — et c'est
elle qui a montré que le seuil « 3 % relatif » du cahier des charges était
inutilisable sur les outsiders (9,7 % relatif pour 0,93 point d'écart).

⚠️ DEUX PIÈGES QUI ONT COÛTÉ DU TEMPS, ÉCRITS ICI POUR NE PLUS LES REPAYER
---------------------------------------------------------------------------
1. KALSHI : les champs entiers historiques `yes_bid` / `yes_ask` / `volume`
   sont rendus **null** par l'API publique — sur 200 marchés ouverts
   échantillonnés, ZÉRO en portait. Conclure « Kalshi n'a pas de prix » serait
   faux : les prix sont dans les champs `*_dollars` en CHAÎNE
   (`no_ask_dollars: "0.5800"`). Ne jamais lire les champs entiers.
2. POLYMARKET : `tag_slug=sports` ne rend que des paris de saison (Ballon
   d'Or, champion 2027). Les marchés PAR MATCH existent, mais sous le tag de
   la ligue et avec un slug de la forme `epl-hul-mun-2026-08-22`. `outcomes`
   et `outcomePrices` sont des CHAÎNES contenant du JSON, pas des listes.

POURQUOI `consensus` ET PAS `sharp`
------------------------------------
Ces marchés sont peu liquides hors des grandes affiches : les matchs de
présaison NFL échantillonnés portaient un écart bid/ask de 4 cents, soit
4 points de probabilité — plus large que le seuil SUSPECT_DATA lui-même. Un
prix aussi large ne peut pas servir de fair price. Il sert à DÉTECTER un
désaccord, ce qui ne demande pas la même précision. D'où : rôle `consensus`,
et un plancher de liquidité en dessous duquel le marché est ignoré.

STATUT JURIDIQUE
----------------
Le plus propre du lot : deux API publiques documentées, sans scraping. Aucun
robots.txt à contourner (`gamma-api.polymarket.com/robots.txt` = 404, c'est
une API, pas un site). Lecture seule, aucune authentification requise pour les
données de marché.
"""
import json
import logging
import os
import re
import urllib.request

from core import daily_quota
from core.source_adapter import Fixture, SourceSpec

log = logging.getLogger("PREDATOR.prediction_markets")

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
POLY_BASE   = "https://gamma-api.polymarket.com"

QUOTA_BUCKET = "prediction_markets"
DAILY_BUDGET = int(os.environ.get("PREDMKT_DAILY_BUDGET", "200"))
TIMEOUT      = int(os.environ.get("PREDMKT_TIMEOUT", "20"))

# Écart bid/ask maximal toléré, en points de probabilité. Au-delà, le « prix »
# du marché est une fourchette plus large que le désaccord qu'on cherche à
# mesurer : il n'apprend rien et il ferait du bruit dans le cross-check.
MAX_SPREAD_PTS = float(os.environ.get("PREDMKT_MAX_SPREAD_PTS", "4.0"))

# ASCII pur — voir core/odds500.py. C'est ICI que le piège a été trouvé :
# gamma-api.polymarket.com rendait 403 sur l'ancienne chaîne accentuée.
_UA = ("PredatorPAIM/1.0 (private non-commercial sports-betting pipeline; "
       "max 1 req/2s)")
_HEADERS = {"User-Agent": _UA, "Accept": "application/json"}

SPEC_KALSHI = SourceSpec(
    name="kalshi", role="consensus", trust=0.5, daily_budget=DAILY_BUDGET,
    langs=("en",), quota_bucket=QUOTA_BUCKET, host="api.elections.kalshi.com",
    legal="API publique documentée, lecture seule, sans clé",
)
SPEC_POLYMARKET = SourceSpec(
    name="polymarket", role="consensus", trust=0.5, daily_budget=DAILY_BUDGET,
    langs=("en",), quota_bucket=QUOTA_BUCKET, host="gamma-api.polymarket.com",
    legal="API publique documentée, lecture seule, sans clé",
)

# Séries Kalshi par ligue. Volontairement courte : uniquement les compétitions
# du portefeuille où un marché par match existe ET est liquide.
KALSHI_SERIES = {
    "nfl": "KXNFLGAME", "nba": "KXNBAGAME",
    "epl": "KXEPLGAME", "ucl": "KXUCLGAME",
}
POLY_TAGS = {"epl": "epl", "nfl": "nfl", "nba": "nba", "ucl": "ucl"}


def _get_json(url: str):
    """GET JSON best-effort. Rend None sur toute panne — jamais d'exception."""
    try:
        req = urllib.request.Request(url, headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        log.warning("prediction_markets: %s — %s", url.split("/")[2], e)
        return None


def _dollars(rec: dict, field: str) -> float | None:
    """Lit un champ `*_dollars` (CHAÎNE) et rend une probabilité 0..1.

    Les champs entiers homonymes (`yes_bid`…) sont rendus null par l'API et ne
    doivent jamais être lus — voir le docstring du module.
    """
    raw = rec.get(field)
    if raw in (None, ""):
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    return v if 0.0 < v < 1.0 else None


def _mid_from_kalshi(rec: dict) -> tuple:
    """(probabilité milieu, spread en points) pour un marché binaire Kalshi.

    Kalshi cote le OUI ; le NON est l'autre côté du même carnet. Quand un côté
    manque, on reconstruit par complément — c'est exact pour un marché binaire
    et ça sauve les marchés cotés d'un seul côté.
    """
    yb, ya = _dollars(rec, "yes_bid_dollars"), _dollars(rec, "yes_ask_dollars")
    nb, na = _dollars(rec, "no_bid_dollars"),  _dollars(rec, "no_ask_dollars")
    if yb is None and na is not None:
        yb = 1.0 - na
    if ya is None and nb is not None:
        ya = 1.0 - nb
    if yb is None or ya is None or ya < yb:
        return None, None
    return (yb + ya) / 2, (ya - yb) * 100


_TICKER_RE = re.compile(r"^(?P<series>[A-Z]+)-(?P<date>\d{2}[A-Z]{3}\d{2})(?P<teams>[A-Z]+)-(?P<side>[A-Z]+)$")


def fetch_kalshi(league: str, limit: int = 60) -> list:
    """Marchés Kalshi d'une ligue → Fixtures avec `odds` en cotes décimales.

    Un événement Kalshi = un match, éclaté en un marché binaire par issue. On
    les regroupe par `event_ticker`, ce qui reconstitue le 1X2 (ou le
    moneyline à deux issues) sans jamais lire un nom d'équipe.
    """
    series = KALSHI_SERIES.get(league)
    if not series:
        return []
    if daily_quota.spent(QUOTA_BUCKET) >= DAILY_BUDGET:
        log.warning("kalshi: budget journalier atteint — cycle ignoré")
        return []

    data = _get_json(f"{KALSHI_BASE}/markets?series_ticker={series}"
                     f"&status=open&limit={int(limit)}")
    daily_quota.add(QUOTA_BUCKET, 1)
    if not data or not isinstance(data.get("markets"), list):
        return []

    events: dict = {}
    for rec in data["markets"]:
        ev = rec.get("event_ticker")
        if not ev:
            continue
        prob, spread = _mid_from_kalshi(rec)
        if prob is None:
            continue
        if spread is not None and spread > MAX_SPREAD_PTS:
            continue                                  # carnet trop large
        events.setdefault(ev, {"legs": [], "close": rec.get("occurrence_datetime")
                                                    or rec.get("expected_expiration_time")})
        events[ev]["legs"].append((rec.get("ticker", ""), prob, rec.get("yes_sub_title") or ""))

    out = []
    for ev, blob in events.items():
        legs = blob["legs"]
        if len(legs) < 2:
            continue                                  # une seule issue cotée
        # Le TIE de Kalshi est le nul : il va au MILIEU, comme le X du 1X2
        # attendu par le moteur.
        tie    = [l for l in legs if l[0].endswith("-TIE")]
        others = [l for l in legs if not l[0].endswith("-TIE")]
        if tie:
            # Un marché à 3 issues dont une seule branche a été écartée (carnet
            # trop large) laisserait un vecteur à 2 issues qui RESSEMBLE à un
            # moneyline. Apparié par structure contre un vrai moneyline, il
            # produirait une correspondance fausse et crédible. On exige donc
            # les trois pattes, ou rien.
            if len(others) != 2:
                continue
            legs = [others[0], tie[0], others[1]]
        elif len(legs) != 2:
            continue                                  # ni 1X2 complet, ni moneyline
        odds = [round(1 / p, 4) for _, p, _ in legs if p > 0]
        if len(odds) != len(legs):
            continue
        kickoff = blob["close"]
        out.append(Fixture(
            source="kalshi", match_id=ev, kickoff=kickoff, league=league,
            home=legs[0][2], away=legs[-1][2], odds=odds, lang="en",
            raw={"tickers": [l[0] for l in legs]},
        ))
    log.info("kalshi[%s]: %d matchs cotés", league, len(out))
    return out


def fetch_polymarket(league: str, limit: int = 60) -> list:
    """Marchés Polymarket par match d'une ligue → Fixtures.

    `outcomes` et `outcomePrices` sont des CHAÎNES contenant du JSON — les
    parser, ne pas les indexer directement.
    """
    tag = POLY_TAGS.get(league)
    if not tag:
        return []
    if daily_quota.spent(QUOTA_BUCKET) >= DAILY_BUDGET:
        log.warning("polymarket: budget journalier atteint — cycle ignoré")
        return []

    data = _get_json(f"{POLY_BASE}/events?limit={int(limit)}&closed=false"
                     f"&tag_slug={tag}&order=startDate&ascending=true")
    daily_quota.add(QUOTA_BUCKET, 1)
    if not isinstance(data, list):
        return []

    out = []
    for ev in data:
        legs = []
        for m in ev.get("markets") or []:
            prices = _parse_json_field(m.get("outcomePrices"))
            outcomes = _parse_json_field(m.get("outcomes"))
            if not prices or not outcomes:
                continue
            # Chaque marché est binaire Oui/Non sur UNE issue du match ;
            # la probabilité de l'issue est le prix du « Yes ».
            try:
                yes = outcomes.index("Yes")
                p = float(prices[yes])
            except (ValueError, IndexError, TypeError):
                continue
            if not 0.0 < p < 1.0:
                continue
            legs.append((m.get("groupItemTitle") or m.get("question") or "", p))
        if len(legs) < 2:
            continue
        # Le nul porte « Draw » dans son libellé — seul endroit où un mot
        # anglais sert ici, et seulement pour ORDONNER, jamais pour apparier.
        draw = [l for l in legs if l[0].lower().startswith("draw")]
        others = [l for l in legs if not l[0].lower().startswith("draw")]
        legs = [others[0], draw[0], others[1]] if (draw and len(others) == 2) else legs
        odds = [round(1 / p, 4) for _, p in legs]
        out.append(Fixture(
            source="polymarket", match_id=ev.get("slug") or "", kickoff=ev.get("endDate"),
            league=league, home=legs[0][0], away=legs[-1][0], odds=odds, lang="en",
            raw={"title": ev.get("title", "")},
        ))
    log.info("polymarket[%s]: %d matchs cotés", league, len(out))
    return out


def _parse_json_field(value):
    """Polymarket sérialise ses listes en chaînes JSON. Tolère déjà-parsé."""
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else None
        except ValueError:
            return None
    return None


def fetch_consensus(league: str) -> list:
    """Les deux marchés d'une ligue, réunis. Best-effort des deux côtés."""
    return fetch_kalshi(league) + fetch_polymarket(league)


def probe() -> tuple:
    """(joignable ?, détail) — pour scripts/ops.py sources."""
    k = _get_json(f"{KALSHI_BASE}/exchange/status")
    p = _get_json(f"{POLY_BASE}/markets?limit=1")
    bits = []
    bits.append("kalshi ok" if k else "kalshi injoignable")
    bits.append("polymarket ok" if isinstance(p, list) else "polymarket injoignable")
    return bool(k or p), " | ".join(bits)
