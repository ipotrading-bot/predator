"""
core/points_cloture.py — la ligne d'un pari NBA contre la ligne de CLÔTURE,
en POINTS (2026-11-17, demande opérateur après « nba perd trop »).

POURQUOI
--------
Un handicap ou un total NBA se joue à cote ~1,93 : ~54 % de gagnants quand le
moteur est exactement juste. Mesuré le 2026-11-17 sur les 35 recommandés de
saison régulière : espérance annoncée +1,06 u, écart-type 5,67 u ; un repli
de 5 u depuis un sommet arrive 54 fois sur 100 avec un moteur parfait, et il
faudrait ~3 900 paris pour voir l'edge au gagné/perdu. Le gagné/perdu ne peut
donc PAS juger la NBA à l'échelle d'un mois : il dit « ça perd » une fois sur
deux, à tort.

Les points pris sur la clôture, eux, tranchent vite. Même jour, 89 handicaps
et totaux réglés : notre ligne est en moyenne 0,90 point meilleure que la
clôture (t +5,6 ; 57 meilleures, 21 égales, 11 pires), et les mêmes paris pris
À la ligne de clôture font 45-44 — la valeur est le point d'avance, pas le
hasard. Avance encore là à la clôture : 35-21 ; perdue : 13-19.

Ce n'est PAS un CLV au sens de la règle 14 : rien n'est écrit en base, aucune
colonne n'est posée. C'est une lecture, à la demande, contre un prix
POSTÉRIEUR réellement observé.

SOURCE
------
ESPN, sans clé : `…/basketball/nba/summary?event=<id>`, bloc `pickcenter`
(lignes d'OUVERTURE et de CLÔTURE DraftKings : `pointSpread`, `total`).
Contrôlée le 2026-11-17 sur la ligue entière (205 matchs) : domicile 49,3 %
contre l'écart, favori 53,7 %, over 47,8 % — une clôture sans biais.

BUDGET (règle 13)
-----------------
Aucun cron ne l'appelle : seul `python scripts/ops.py nba [jours]`, à la main.
Une requête par JOUR de match (scoreboard, jamais de plage — règle 15) et une
par MATCH portant un pari ; ~110 pour 30 jours. Plafond dur par appel :
REQUETES_MAX. Lecture directe, hors des compteurs `daily_quota` des runners.

CRITÈRE DE RETRAIT (règle 13)
-----------------------------
Revue le REVUE_LE. La commande SORT si la couverture (paris dont le match
porte une ligne de clôture lisible) tombe sous COUVERTURE_MIN — ESPN aura
retiré ou vidé `pickcenter` — ou si personne ne l'a relue d'ici là.
"""
import math
import re

from core.stats_utils import p_breakeven, wilson_ci

REQUETES_MAX = 250          # par appel d'ops.py nba
COUVERTURE_MIN = 0.80       # sous ce taux de paris couverts, la commande sort
REVUE_LE = "2026-12-17"
CHEMIN_ESPN = "basketball/nba"

_NOMBRE = re.compile(r"[+-]?\d+(?:\.\d+)?")


def _nombre(texte) -> float | None:
    """« -8.5 », « +1.5 », « o228.5 », « u228.5 » → le nombre ; None sinon."""
    m = _NOMBRE.search(str(texte or ""))
    return float(m.group(0)) if m else None


def lignes_du_resume(resume: dict | None) -> dict | None:
    """Lignes DraftKings d'un résumé ESPN : handicap du DOMICILE et total, à
    l'ouverture et à la clôture. None si la clôture n'est pas lisible — on ne
    mesure pas contre une ligne devinée."""
    for bloc in (resume or {}).get("pickcenter") or []:
        try:
            dom = bloc["pointSpread"]["home"]
            tot = bloc["total"]["over"]
            cloture = {"handicap_dom": _nombre(dom["close"]["line"]),
                       "total": _nombre(tot["close"]["line"])}
        except (KeyError, TypeError):
            continue
        if None in cloture.values():
            continue
        cloture["handicap_dom_ouverture"] = _nombre((dom.get("open") or {}).get("line"))
        cloture["total_ouverture"] = _nombre((tot.get("open") or {}).get("line"))
        return cloture
    return None


def ecart_heures(instant_a, instant_b) -> float | None:
    """Heures entre deux instants ISO (« Z » accepté), None si illisible."""
    from datetime import datetime, timezone
    try:
        a = datetime.fromisoformat(str(instant_a).replace("Z", "+00:00"))
        b = datetime.fromisoformat(str(instant_b).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    a = a if a.tzinfo else a.replace(tzinfo=timezone.utc)
    b = b if b.tzinfo else b.replace(tzinfo=timezone.utc)
    return abs((a - b).total_seconds()) / 3600


# Deux franchises se rencontrent parfois deux fois en trois jours : le match
# d'un pari est celui dont le coup d'envoi ESPN tombe à moins de FENETRE_H de
# l'heure du signal, et le PLUS PROCHE s'il y en a deux.
FENETRE_H = 12.0


def le_plus_proche(candidats: list[tuple], instant) -> tuple | None:
    """Parmi des (date ESPN, …), celui dont la date est la plus proche de
    `instant`, à moins de FENETRE_H ; None si aucun."""
    dates = [(ecart_heures(c[0], instant), c) for c in candidats]
    dates = [(e, c) for e, c in dates if e is not None and e <= FENETRE_H]
    return min(dates, key=lambda ec: ec[0])[1] if dates else None


def _camp(market_key: str) -> str | None:
    """« home » / « away » d'un handicap, None pour tout autre marché."""
    return {"spreads_home": "home", "spreads_away": "away"}.get(market_key or "")


def ligne_de_cloture(market_key: str, lignes: dict, domicile_en_premier: bool = True) -> float | None:
    """La ligne de clôture du MÊME pari : le total, ou le handicap du camp
    joué. `domicile_en_premier` : le « home » du signal (premier nom de
    « A vs B ») est bien le domicile d'ESPN ; sinon le camp s'inverse."""
    if (market_key or "").startswith("totals_"):
        return lignes.get("total")
    camp = _camp(market_key)
    if camp is None or lignes.get("handicap_dom") is None:
        return None
    joue_le_domicile = (camp == "home") == domicile_en_premier
    return lignes["handicap_dom"] if joue_le_domicile else -lignes["handicap_dom"]


def points_pris(market_key: str, ligne: float, lignes: dict,
                domicile_en_premier: bool = True) -> float | None:
    """Points gagnés sur la clôture, POSITIFS quand notre ligne est meilleure.

        Over 220.5, clôture 222.5   → +2   (il faut moins de points)
        Under 220.5, clôture 219.5  → +1
        −4.5, clôture −8.5          → +4   (on donne moins de points)
        +8.5, clôture +6.5          → +2   (on en reçoit plus)

    None pour un h2h ou une clôture illisible."""
    cloture = ligne_de_cloture(market_key, lignes, domicile_en_premier)
    if cloture is None or ligne is None:
        return None
    if market_key == "totals_over":
        return cloture - ligne
    return ligne - cloture


def marge(market_key: str, ligne: float, score_dom: int, score_ext: int,
          domicile_en_premier: bool = True) -> float | None:
    """Points au-delà de la ligne, du côté du pari : > 0 gagné, < 0 perdu,
    0 remboursé. Sert à rejouer le pari à la ligne de clôture."""
    if ligne is None:
        return None
    if market_key == "totals_over":
        return score_dom + score_ext - ligne
    if market_key == "totals_under":
        return ligne - (score_dom + score_ext)
    camp = _camp(market_key)
    if camp is None:
        return None
    ecart = score_dom - score_ext
    return (ecart if (camp == "home") == domicile_en_premier else -ecart) + ligne


def mesurer(market_key: str, ligne: float, lignes: dict, score_dom: int, score_ext: int,
            domicile_en_premier: bool = True) -> dict | None:
    """{points, marge_cloture} d'un pari, None s'il n'est pas mesurable."""
    pts = points_pris(market_key, ligne, lignes, domicile_en_premier)
    if pts is None:
        return None
    cloture = ligne_de_cloture(market_key, lignes, domicile_en_premier)
    return {"points": pts,
            "marge_cloture": marge(market_key, cloture, score_dom, score_ext, domicile_en_premier)}


def bilan(mesures: list[dict], tax_rate: float) -> dict:
    """Un groupe de paris mesurés ({outcome, odds, points, marge_cloture}).

    Rend le gagné/perdu avec Wilson ET point mort (règle 7), les points pris
    sur la clôture (moyenne, t de Student contre 0, meilleures/égales/pires)
    et le bilan des MÊMES paris rejoués à la ligne de clôture."""
    decides = [m for m in mesures if m.get("outcome") in ("WIN", "LOSS")]
    n = len(decides)
    if n == 0:
        return {"n": 0}
    gagnes = sum(1 for m in decides if m["outcome"] == "WIN")
    bas, haut = wilson_ci(gagnes, n)
    cotes = [float(m["odds"]) for m in decides if m.get("odds")]
    pts = [m["points"] for m in decides]
    moyenne = sum(pts) / n
    t = None
    if n > 1:
        variance = sum((x - moyenne) ** 2 for x in pts) / (n - 1)
        if variance > 0:
            t = moyenne / math.sqrt(variance / n)
    rejoues = [m["marge_cloture"] for m in decides if m.get("marge_cloture")]
    return {"n": n, "gagnes": gagnes, "perdus": n - gagnes,
            "reussite": gagnes / n, "wilson_bas": bas, "wilson_haut": haut,
            "point_mort": p_breakeven(sum(cotes) / len(cotes), tax_rate) if cotes else None,
            "points_moyens": moyenne, "t": t,
            "meilleures": sum(1 for x in pts if x > 0),
            "egales": sum(1 for x in pts if x == 0),
            "pires": sum(1 for x in pts if x < 0),
            "cloture_gagnes": sum(1 for x in rejoues if x > 0),
            "cloture_perdus": sum(1 for x in rejoues if x < 0)}


def ligne_de_bilan(nom: str, b: dict) -> str:
    """Une ligne de tableau, chiffres seuls."""
    if not b.get("n"):
        return f"{nom:<22} n=0"
    pm = f"{b['point_mort'] * 100:.1f}%" if b.get("point_mort") is not None else "–"
    t = f"{b['t']:+.1f}" if b.get("t") is not None else "–"
    return (f"{nom:<22} {b['n']:>3}  {b['gagnes']:>2}-{b['perdus']:<2}  "
            f"{b['reussite'] * 100:4.1f}% [{b['wilson_bas'] * 100:.0f}–{b['wilson_haut'] * 100:.0f}]  "
            f"pm {pm:<6} {b['points_moyens']:+5.2f} pt (t {t})  "
            f"{b['meilleures']}/{b['egales']}/{b['pires']:<3} "
            f"{b['cloture_gagnes']}-{b['cloture_perdus']}")


EN_TETE = (f"{'':<22} {'n':>3}  {'bilan':<5}  {'réussite [IC95]':<15}  {'point mort':<9} "
           f"{'pts/clôture':<16} mieux/égal/pire  à la clôture")
