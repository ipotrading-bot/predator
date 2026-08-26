"""
core/exchange_match.py — apparier un match du slate avec un marché d'exchange.

POURQUOI UN MODULE. Ces deux fonctions vivaient dans run_engine.py, où seul
l'enrichissement des prix les appelait. Depuis le 2026-08-26 la capture de
closing line en a besoin AUSSI (core/closing_line.capture_from_exchange) —
et `core` ne doit pas importer la racine. Elles sont donc ici, sans réseau ni
état, testables telles quelles.

POURQUOI PAS DANS core/source_adapter.py. Ce module-là pose une doctrine
explicite — apparier par temps + ligue + STRUCTURE de cotes, JAMAIS par nom.
`_lookup_exchange` fait exactement l'inverse : c'est l'appariement historique
par nom du chemin Betfair/Matchbook, qui n'a ni le coup d'envoi ni la ligue
côté exchange. Les mélanger rendrait la doctrine de source_adapter illisible.

Mesuré le 2026-08-20 sur 13 matchs odds-api.io contre 53 marchés Matchbook :
la clé exacte en appariait 0, le rapprochement flou 8.
"""
import logging

from core.paim_engine import strict_team_match

log = logging.getLogger("PREDATOR.exchange")


def flip_exchange_prices(row: dict) -> dict:
    """Retourne les prix d'exchange d'un match trouvé dans l'autre sens.

    L'exchange peut nommer le match « B vs A » là où la source soft dit
    « A vs B ». Inverser 1 et 2 ne suffit pas : le handicap porte le SIGNE de
    l'équipe qui le concède, donc il s'inverse aussi. Un handicap laissé tel
    quel donnerait un edge calculé contre la mauvaise ligne — faux, et
    silencieux. Les totals, eux, sont symétriques et se recopient.
    """
    out = {"1": row["2"], "X": row.get("X", 0.0), "2": row["1"],
           "_source": row.get("_source", "betfair")}
    if row.get("totals"):
        out["totals"] = row["totals"]
    sp = row.get("spreads")
    if sp:
        out["spreads"] = {"home": sp["away"], "away": sp["home"],
                          "point": sp.get("away_point", -sp["point"]),
                          "away_point": sp["point"]}
    return out



def lookup_exchange(m: dict, prices: dict) -> dict | None:
    """Retrouve un match dans les prix d'exchange, malgré les noms.

    Le rapprochement par clé EXACTE ne marche pratiquement jamais entre deux
    fournisseurs : mesuré le 2026-08-20 sur 13 matchs odds-api.io contre 53
    marchés Matchbook, la clé exacte en appariait **0**, le rapprochement
    flou **8**. « Cde Juventud Italiana » contre « Club Juventud Italiana »,
    « CSD Macara » contre « Deportivo Macara »… C'est ce seul détail qui
    tenait le pipeline à zéro signal malgré deux sources en bon état.

    Ordre : clé exacte, clé exacte inversée, puis `strict_team_match` (le
    rapprochement déjà utilisé partout dans ce projet). En flou, on n'accepte
    qu'un candidat UNIQUE : deux prétendants signifient qu'on ne sait pas
    lequel est le bon, et poser le mauvais prix sharp donnerait un edge faux
    sans rien casser de visible.
    """
    h = m.get("home", "").strip()
    a = m.get("away", "").strip()
    if not h or not a:
        return None
    hl, al = h.lower(), a.lower()

    hit = prices.get(f"{hl}_{al}")
    if hit:
        return hit
    hit = prices.get(f"{al}_{hl}")
    if hit:
        return flip_exchange_prices(hit)

    forward, reverse = [], []
    for row in prices.values():
        rh, ra = str(row.get("home", "")).strip(), str(row.get("away", "")).strip()
        # `strict_team_match` renvoie True dès qu'un nom est VIDE (voir
        # core/paim_engine.py) : sans ce garde, une ligne de prix sans
        # home/away s'apparierait à n'importe quel match. Le seuil de
        # longueur écarte de même les fragments trop courts, qu'un simple
        # test d'inclusion ferait matcher avec tout ("a" est dans
        # "barcelona").
        if len(rh) < 3 or len(ra) < 3 or len(h) < 3 or len(a) < 3:
            continue
        if strict_team_match(h, rh) and strict_team_match(a, ra):
            forward.append(row)
        elif strict_team_match(h, ra) and strict_team_match(a, rh):
            reverse.append(row)
    if len(forward) == 1 and not reverse:
        return forward[0]
    if len(reverse) == 1 and not forward:
        return flip_exchange_prices(reverse[0])
    return None
