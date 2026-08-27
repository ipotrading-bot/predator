"""
scripts/replay_ledger_executable.py — PHASE A0 — Rejouer le ledger au prix
RÉELLEMENT EXÉCUTABLE.

CE QUE CE SCRIPT MESURE
-----------------------
Le moteur compare une référence sharp au « prix soft ». Pour le football, ce
« prix soft » n'est PAS un prix qu'un book affiche : `core.math_engine.to_binary`
rend `calc_dnb(o1, o2, oX)`, c'est-à-dire un Draw No Bet **dévigorisé** — la
marge du book a été retirée avant la comparaison. L'edge publié mesure donc une
divergence d'opinion entre books, jamais un rendement atteignable.

Le prix qu'on peut réellement jouer est le DNB **synthétique** : miser 1/oX sur
le nul et le reste sur l'équipe, ce qui rend le nul neutre. Sa cote vaut

    exec = o1 · (oX − 1) / oX

Ce script recalcule chaque ligne réglée du ledger avec `exec` à la place du prix
dévigorisé, et rend : ROI réel vs ROI recalculé, distribution des edges avant et
après, nombre de lignes qui passeraient encore MIN_EDGE, et taux de résolution
(réglés / (réglés + expired)) — le tout par sport et par bande d'edge.

CE QUI EST TOUCHÉ, ET CE QUI NE L'EST PAS
-----------------------------------------
Seul le **football en h2h** est concerné : c'est la seule branche de `to_binary`
qui dévigorise. Pour tous les autres marchés (spreads, totals, quel que soit le
sport) et pour le h2h hors football, `to_binary` et `_process_totals` /
`_process_spreads` stockent la cote SOFT BRUTE — déjà exécutable, prélèvement
nul. Ce n'est pas une bonne nouvelle : sur le ledger d'août 2026, 53 des 114
lignes réglées sont du football h2h, et CLAUDE.md note que 100 % des signaux
récents sont du football.

POURQUOI UN PRÉLÈVEMENT MODÉLISÉ, ET PAS LE 1X2 BRUT
----------------------------------------------------
Vérifié en base le 2026-08-27 : NI `signals` NI `ai_learning_ledger` ne
conservent le 1X2 soft brut. Seul le DNB dévigorisé (`xbet_odd` → `odds`)
survit. Le 1X2 d'origine est donc irrécupérable pour une ligne historique, et
aucune astuce d'algèbre ne le rend : à `xbet_odd` donné il reste deux inconnues
(la marge M = 1/o1 + 1/oX + 1/o2 du book, et la probabilité implicite du nul
qx = 1/oX). Sous dévigorisation multiplicative, le rapport vaut exactement

    h = exec / dévigorisé = (1 − qx) / (M − qx)

`h` ne dépend donc que de la marge du book et du prix du nul — deux grandeurs
stables, qui se MESURENT sur un slate courant sans rien savoir de l'historique.

MESURE DU 2026-08-27 (aucune extrapolation)
-------------------------------------------
`h` a été mesuré directement — pas via la formule ci-dessus mais en comparant,
match par match, `calc_dnb()` du dépôt à `exec`, sur les 1X2 bruts du jour :

  odds-api.io (1xbet seul), football, 37 matchs — REPRODUIT LE 2026-08-27
    · h                                                p10 0.8720 | med 0.8929 | p90 0.9343
    · marge M du 1X2                                   p10 1.0543 | med 1.1012

  api-sports, football, 30 matchs, 3 à 12 books soft par match — MESURÉ AUSSI
  le 2026-08-27, en 7 requêtes `/odds?date=` (l'en-tête de quota annonçait
  encore 99 disponibles : la réserve de settlement n'a pas été entamée). Ces
  requêtes ont été faites EN DEHORS de `core.api_sports.fetch_sport`, donc sans
  passer par le compteur `meta` — à refaire par `--calibrate-from` sur une
  capture, plutôt qu'en rappelant l'API :
    · h sur le 1X2 line-shoppé (comportement ACTUEL)   p10 0.9199 | med 0.9296 | p90 0.9821
    · h sur le meilleur DNB PAR BOOK (cible de A1)     p10 0.9082 | med 0.9244 | p90 0.9806
    · surestimation du 1X2 shoppé vs par-book          med +0.73 % | p90 +2.46 %

Le défaut du script est le chiffre **odds-api.io**, parce qu'il est le plus
SÉVÈRE des deux : si une ligne survit sous ce prélèvement, elle survit sous
n'importe quel autre du panel. Un argument milite pour l'autre valeur et mérite
d'être posé avant A6 — api-sports est la source qui porte RÉELLEMENT les signaux
football (odds-api.io ne sert aucun prix sharp, cf. CLAUDE.md), donc 0.9244 est
probablement plus proche du prix qu'une exécution obtiendrait. Le choix ne change
pas la conclusion : `--haircut` rejoue sous l'autre hypothèse, et la table de
sensibilité couvre TOUTE la bande 0.8720 → 0.9806, sur laquelle le verdict tient.

Le 1X2 line-shoppé mérite d'être noté à part : agréger le meilleur prix par
issue à travers 10 books fabrique un 1X2 que personne n'offre, et surestime le
prix exécutable de 0,73 point médian (2,46 au p90). C'est exactement ce que A1
corrige dans `core.api_sports.extract_prices`.

DEUX UNITÉS D'EDGE COHABITENT DANS LE LEDGER
--------------------------------------------
Mesuré le 2026-08-27 : sur 303 lignes portant `sharp_prob`, seules celles du
2026-08-22 et après vérifient l'identité du moteur actuel
`initial_edge == (sharp_prob × odds − 1) × 100`. Avant cette date, l'écart
atteint 13 points et va TOUJOURS dans le même sens — `initial_edge` positif là
où l'EV vraie est négative (odds 1,08 / sharp_prob 0,858 : +5,88 % écrit,
−7,34 % réel). C'est la signature du RATIO DE PRIX que `compute_alpha` rendait
jusqu'à la refonte du 2026-08-22 (voir sa docstring). Le partage est net :

  jusqu'au 2026-08-21 : 54 lignes en unité EV, 58 en ratio de prix
  à partir du 2026-08-22 : 201 lignes en unité EV, 2 en ratio, 0 après le 23

Sur les 60 lignes en ancienne unité, 45 portent un `initial_edge` POSITIF pour
une EV réelle NÉGATIVE : ce ne sont pas des edges plus petits qu'annoncé, ce
sont des paris que le moteur d'aujourd'hui n'aurait pas émis du tout.

Conséquence directe pour ce rapport : `initial_edge` N'EST PAS comparable d'un
bout à l'autre du ledger, et le pooler donnerait un « avant » qui n'existe dans
aucune des deux époques. L'« avant » est donc RECALCULÉ pour toutes les lignes
avec la formule actuelle sur le prix stocké — `edge_base` — et `initial_edge`
n'est rendu que pour montrer la dérive, avec son unité étiquetée. La tolérance
de détection (EDGE_UNIT_TOL) vaut 1 point : `signals.xbet_odd` est un
`numeric(6,2)`, l'arrondi de la cote à deux décimales déplace à lui seul l'edge
d'un cote courte de ±0,45 point (mesuré : écarts post-refonte dans [−0,37 ;
+0,44], médiane 0,00).

⚠️ Cela concerne aussi `core/learning_layer.py`, qui lit ce ledger sans
distinguer les deux unités — hors périmètre de A0, signalé ici parce que la
mesure le montre.

CONTRAT
-------
LECTURE SEULE. Le script n'écrit rien en base, ne prend aucune décision, ne
touche à aucun seuil. Il ne fait AUCUN appel réseau hors de la lecture Supabase :
la calibration se rejoue hors-ligne via `--calibrate-from`, sur un fichier JSON
de 1X2 bruts capturé par ailleurs (voir `measure_haircut`).
"""
import argparse
import json
import logging
import statistics

from core.constants import EV_EDGE_FLOOR, TAX_RATE, roi_net_of_tax
from core.math_engine import calc_dnb
from core.stats_utils import p_breakeven, wilson_ci
from core.paim_engine import MIN_EDGE

log = logging.getLogger("PREDATOR.replay_ledger_executable")

# Prélèvement médian MESURÉ ET REPRODUIT le 2026-08-27 sur odds-api.io
# (37 matchs de football, 1xbet), DNB synthétique exécutable rapporté au DNB
# dévigorisé — voir la docstring du module. Ce n'est ni une estimation prudente
# ni un arrondi de confort : c'est la médiane observée.
#
# Les bornes de sensibilité couvrent DEUX sources et non les seuls déciles
# d'odds-api.io : le plancher est son p10, le plafond est le book le moins
# margé du panel api-sports. Une conclusion qui tient de 0.8720 à 0.9806 ne
# dépend d'aucune des deux mesures en particulier.
HAIRCUT_DEFAULT = 0.8929
HAIRCUT_P10     = 0.8720   # book le plus margé mesuré (odds-api.io, p10)
HAIRCUT_P90     = 0.9806   # book le moins margé mesuré (api-sports, par book)

# Taux réel de retenue sur le GAIN NET (cf. core/tax_engine.py). `TAX_RATE`
# vaut 0.0 dans le dépôt au moment d'écrire ces lignes ; A2 doit le rétablir.
# Le rapport affiche les deux colonnes pour que l'écart soit visible AVANT
# qu'on touche à la constante.
TAX_REAL = 0.20

# Écart toléré entre `initial_edge` et l'identité du moteur actuel avant de
# classer la ligne comme « ancienne unité ». Calé sur l'arrondi de la cote :
# `signals.xbet_odd` est un numeric(6,2), ce qui suffit à déplacer l'edge d'une
# cote courte de ±0,45 point. Mesuré post-refonte : écarts dans [−0,37 ; +0,44].
EDGE_UNIT_TOL = 1.0

# Bandes d'edge, exprimées dans l'unité EV ACTUELLE (`edge_base`).
EDGE_BANDS = [(-1e9, 0.0), (0.0, 1.5), (1.5, 3.0), (3.0, 5.0),
              (5.0, 8.0), (8.0, 15.0), (15.0, 1e9)]

# Les marchés où `to_binary` dévigorise — donc les seuls dont le prix stocké
# n'est pas exécutable. Tout le reste stocke la cote soft brute.
DEVIGGED = {("soccer", "h2h")}


# ── Prix exécutable ──────────────────────────────────────────────────────

def is_devigged(sport: str, market_type: str) -> bool:
    """Le prix stocké pour cette ligne est-il dévigorisé (donc non jouable) ?"""
    return (str(sport or ""), str(market_type or "")) in DEVIGGED


def executable_odd(stored_odd: float, sport: str, market_type: str,
                   haircut: float = HAIRCUT_DEFAULT) -> float:
    """
    Cote réellement exécutable pour une ligne du ledger.

    Football h2h : le prix stocké est un DNB dévigorisé, on lui applique le
    prélèvement mesuré (`haircut`) pour retrouver le DNB synthétique
    o1·(oX−1)/oX que le book offre vraiment.
    Partout ailleurs : la cote stockée est déjà la cote soft brute, on la rend
    telle quelle.
    """
    o = float(stored_odd or 0.0)
    if o <= 1.0:
        return 0.0
    if is_devigged(sport, market_type):
        return round(o * haircut, 4)
    return round(o, 4)


def sharp_prob_of(row: dict) -> float:
    """
    Probabilité sharp de la ligne. Quand `sharp_prob` manque (lignes d'avant
    migrate_v9_7), on la reconstruit depuis edge et cote — c'est exactement
    l'hypothèse qu'avait faite `compute_alpha` au moment du scan, donc rien
    n'est inventé : fair = odds/(1+edge/100), p = 1/fair.
    """
    p = row.get("sharp_prob")
    if p:
        return float(p)
    odds = float(row.get("odds") or 0.0)
    edge = row.get("initial_edge")
    if not odds or edge is None:
        return 0.0
    fair = odds / (1 + float(edge) / 100.0)
    return 1.0 / fair if fair > 1.01 else 0.0


def edge_of(sharp_prob: float, odd: float) -> float:
    """EV en % — même formule que `core.paim_engine.compute_alpha`."""
    if sharp_prob <= 0.0 or odd <= 1.01:
        return 0.0
    return round((sharp_prob * odd - 1) * 100, 2)


# ── Normalisation d'une ligne ────────────────────────────────────────────

def normalize(row: dict, haircut: float) -> dict | None:
    """Ligne de ledger → forme commune, avec son prix exécutable et l'edge
    recalculé. Rend None si la ligne n'est pas exploitable."""
    odds = float(row.get("odds") or 0.0)
    if odds <= 1.01:
        return None
    sport   = str(row.get("sport") or "")
    market  = str(row.get("market_type") or "")
    outcome = str(row.get("outcome") or "")
    p       = sharp_prob_of(row)
    ex      = executable_odd(odds, sport, market, haircut)
    stored = float(row.get("initial_edge") or 0.0)
    base   = edge_of(p, odds)
    return {
        "sport":        sport,
        "market_type":  market,
        "outcome":      outcome,
        "date":         (row.get("created_at") or "")[:10],
        "odds":         odds,
        "exec_odds":    ex,
        "sharp_prob":   p,
        # `edge_stored` = ce qui est ÉCRIT, dans l'unité de son époque.
        # `edge_base`   = le même prix relu avec la formule d'aujourd'hui —
        #                 c'est le seul « avant » comparable au « après ».
        "edge_stored":  stored,
        "edge_base":    base,
        "edge_replay":  edge_of(p, ex),
        "unite_ev":     abs(base - stored) <= EDGE_UNIT_TOL,
        "kelly_pct":    float(row.get("kelly_pct") or 0.0),
        "devigged":     is_devigged(sport, market),
    }


# ── ROI ──────────────────────────────────────────────────────────────────

def _profit(outcome: str, odd: float, tax: float) -> float | None:
    """Profit d'une mise de 1 unité. None = ligne non décisive (elle ne doit
    entrer ni au numérateur ni au dénominateur d'un ROI)."""
    if outcome == "WIN":
        return (odd - 1.0) * (1.0 - tax)
    if outcome == "LOSS":
        return -1.0
    if outcome == "PUSH":
        # DNB : le nul rembourse la mise, des DEUX côtés du calcul. Neutre,
        # et neutre au prix exécutable aussi — la jambe « nul » rend
        # exactement la mise totale par construction.
        return 0.0
    return None


def roi(records: list[dict], odd_key: str, tax: float = 0.0,
        kelly: bool = False) -> dict:
    """
    ROI sur les lignes décisives. `odd_key` choisit la cote ('odds' = telle
    qu'écrite au scan, 'exec_odds' = réellement jouable).

    `kelly=True` pondère chaque ligne par `kelly_pct` au lieu d'une mise plate.
    Une ligne sans `kelly_pct` est ignorée dans ce mode plutôt que de recevoir
    une mise inventée.
    """
    staked = profit = 0.0
    n = wins = pushes = 0
    for r in records:
        pr = _profit(r["outcome"], r[odd_key], tax)
        if pr is None:
            continue
        stake = (r["kelly_pct"] / 100.0) if kelly else 1.0
        if kelly and stake <= 0.0:
            continue
        n += 1
        if r["outcome"] == "WIN":
            wins += 1
        elif r["outcome"] == "PUSH":
            pushes += 1
        staked += stake
        profit += pr * stake
    return {
        "n": n, "wins": wins, "pushes": pushes,
        "staked": round(staked, 4),
        "profit": round(profit, 4),
        "roi_pct": round(profit / staked * 100, 2) if staked > 0 else None,
        "win_rate": round(wins / (n - pushes) * 100, 2) if (n - pushes) > 0 else None,
    }


# ── Distributions ────────────────────────────────────────────────────────

def _pct(values: list[float], q: float) -> float:
    s = sorted(values)
    return s[min(len(s) - 1, max(0, round(q * (len(s) - 1))))]


def distribution(values: list[float]) -> dict:
    if not values:
        return {"n": 0}
    return {
        "n": len(values),
        "p10": round(_pct(values, 0.10), 2),
        "median": round(_pct(values, 0.50), 2),
        "p90": round(_pct(values, 0.90), 2),
        "mean": round(statistics.mean(values), 2),
        "min": round(min(values), 2),
        "max": round(max(values), 2),
    }


def _band_label(lo: float, hi: float) -> str:
    if lo <= -1e9:
        return "< 0%"
    if hi >= 1e9:
        return f">= {lo:.1f}%"
    return f"{lo:.1f} – {hi:.1f}%"


def band_of(edge: float) -> str:
    for lo, hi in EDGE_BANDS:
        if lo <= edge < hi:
            return _band_label(lo, hi)
    return "< 0%"


def survivors(records: list[dict], floor: float) -> dict:
    """
    Combien de lignes passeraient encore un plancher donné.

    `before` se lit sur `edge_base` (prix stocké, formule d'AUJOURD'HUI) et non
    sur `initial_edge` : comparer un plancher actuel à un edge écrit dans
    l'unité d'avant le 2026-08-22 mélangerait deux échelles. `emis_a_lepoque`
    dit, lui, combien de lignes leur époque avait laissé passer — l'écart entre
    les deux colonnes est la dérive d'unité, pas l'effet du prix exécutable.
    """
    before = sum(1 for r in records if r["edge_base"] >= floor)
    after  = sum(1 for r in records if r["edge_replay"] >= floor)
    return {"floor": floor, "n": len(records), "before": before, "after": after,
            "emis_a_lepoque": sum(1 for r in records if r["edge_stored"] >= floor),
            "kept_pct": round(after / before * 100, 1) if before else None}


# Un signal a REÇU un résultat (ledger : outcome ; signals : status).
_RESOLU = {"WIN", "LOSS", "PUSH", "settled"}


def resolution_rate(rows: list[dict], field: str = "outcome") -> dict:
    """
    réglés / (réglés + expired) — le biais de survie que /performance cache
    aujourd'hui : un signal purgé sans score sort en `expired` et disparaît des
    stats, alors que ces lignes-là sont précisément celles dont l'edge était le
    plus douteux.

    `field` vaut `outcome` sur le ledger et `status` sur `signals`, qui portent
    le même fait sous deux vocabulaires. `active`/`closed` n'entrent nulle part :
    ni résultat, ni abandon — des états intermédiaires, et les compter au
    dénominateur ferait passer un run récent pour une panne de règlement.
    """
    settled = sum(1 for r in rows if str(r.get(field)) in _RESOLU)
    expired = sum(1 for r in rows if str(r.get(field)) == "expired")
    denom = settled + expired
    return {"settled": settled, "expired": expired, "denom": denom,
            "rate_pct": round(settled / denom * 100, 1) if denom else None}


# ── Calibration hors-ligne du prélèvement ────────────────────────────────

def measure_haircut(raw_1x2: list[dict]) -> dict:
    """
    Distribution du prélèvement `h` sur une liste de 1X2 SOFT BRUTS
    (`[{"1": o1, "X": ox, "2": o2}, …]`).

    Fonction PURE, sans réseau : elle compare, côté favori, le `calc_dnb()` du
    dépôt — le prix que `to_binary` aurait stocké — au DNB synthétique
    réellement jouable. C'est la mesure qui produit HAIRCUT_DEFAULT ; la
    rejouer sur une capture plus fraîche ne demande qu'un dump JSON.
    """
    hs: list[float] = []
    for o in raw_1x2 or []:
        try:
            o1, ox, o2 = float(o.get("1") or 0), float(o.get("X") or 0), float(o.get("2") or 0)
        except (TypeError, ValueError):
            continue
        if min(o1, ox, o2) <= 1.01:
            continue
        fav, opp = (o1, o2) if o1 <= o2 else (o2, o1)
        devig = calc_dnb(fav, opp, ox)
        ex = fav * (ox - 1) / ox
        if devig <= 1.01 or ex <= 1.0:
            continue
        hs.append(ex / devig)
    if not hs:
        return {"n": 0}
    return {"n": len(hs),
            "p10": round(_pct(hs, 0.10), 4),
            "median": round(_pct(hs, 0.50), 4),
            "p90": round(_pct(hs, 0.90), 4),
            "mean": round(statistics.mean(hs), 4)}


# ── A6 — calibration par bandes d'EV RECALCULÉE ──────────────────────────

# Bornes des bandes, dans la NOUVELLE unité (EV au prix exécutable). Volontai-
# rement régulières : choisir des bornes qui font qualifier une bande, c'est
# fabriquer le résultat qu'on prétend mesurer.
CALIB_BORNES = [-1e9, -7.5, -5.0, -2.5, 0.0, 2.5, 5.0, 7.5, 10.0, 1e9]

# Taille minimale d'une bande pour qu'un verdict soit prononcé. Reprise de
# `core.learning_layer._MIN_SAMPLES` : le dépôt refuse déjà de conclure sous
# 30 réglés, et A6 n'a aucune raison d'être plus permissif que la couche
# d'apprentissage qu'il calibre.
CALIB_MIN_N = 30


def calibration_bands(records: list[dict], bornes: list[float] | None = None,
                      min_n: int = CALIB_MIN_N) -> list[dict]:
    """
    Par bande d'EV RECALCULÉE : ce qu'un seuil placé là aurait produit.

    Une bande QUALIFIE quand les trois conditions tiennent ensemble :
      · n >= `min_n` réglés (WIN/LOSS) — sous ce seuil on ne conclut pas ;
      · ROI réalisé NET DE TAXE strictement positif ;
      · borne BASSE de Wilson au-dessus du point mort net de taxe, calculé à
        la cote EXÉCUTABLE moyenne de la bande.

    Les trois sont nécessaires et aucune ne remplace les autres : un ROI
    positif sur 12 paris ne prouve rien, et un taux de réussite au-dessus du
    point mort sans borne de Wilson qui suive est du bruit qu'on a eu la
    chance de voir dans le bon sens.
    """
    bornes = bornes or CALIB_BORNES
    out = []
    for lo, hi in zip(bornes[:-1], bornes[1:]):
        dans = [r for r in records if lo <= r["edge_replay"] < hi]
        dec = [r for r in dans if r["outcome"] in ("WIN", "LOSS")]
        n = len(dec)
        wins = sum(1 for r in dec if r["outcome"] == "WIN")
        cotes = [r["exec_odds"] for r in dec if r["exec_odds"] > 1.01]
        avg_odds = sum(cotes) / len(cotes) if cotes else None
        # ROI au prix EXÉCUTABLE : `roi_net_of_tax` lit la clé `odds`, on lui
        # présente donc la cote jouable sous ce nom.
        roi_net = roi_net_of_tax(
            [{"outcome": r["outcome"], "odds": r["exec_odds"],
              "kelly_pct": r["kelly_pct"] or 1.0} for r in dec], TAX_RATE)
        lo_w, hi_w = wilson_ci(wins, n) if n else (None, None)
        be = p_breakeven(avg_odds, TAX_RATE) if avg_odds else None
        qualifie = bool(
            n >= min_n and roi_net is not None and roi_net > 0
            and lo_w is not None and be is not None and lo_w > be)
        out.append({
            "plancher": lo, "plafond": hi,
            "n_total": len(dans), "n": n, "wins": wins,
            "hit_rate": wins / n if n else None,
            "avg_odds": avg_odds,
            "roi_net": roi_net,
            "wilson_lower": lo_w, "wilson_upper": hi_w,
            "p_breakeven": be,
            "qualifie": qualifie,
            # Une bande est PROUVÉE PERDANTE quand sa borne HAUTE reste sous
            # le point mort : ce n'est plus « pas prouvé rentable », c'est
            # prouvé non rentable. C'est ce qui fonde un plafond.
            "prouvee_perdante": bool(
                n >= min_n and hi_w is not None and be is not None and hi_w < be),
        })
    return out


def seuil_propose(bandes: list[dict]) -> float | None:
    """Plancher de la bande QUALIFIANTE la plus basse, ou None si aucune.

    None est un RÉSULTAT, pas un échec de la mesure : il dit qu'aucun niveau
    d'EV, sur ces données, ne se montre rentable de façon prouvée. Inventer
    une valeur pour que le moteur émette reviendrait à recalibrer jusqu'à
    retrouver l'ensemble de paris perdants sous une autre étiquette.
    """
    for b in bandes:
        if b["qualifie"]:
            return b["plancher"]
    return None


def suspect_edge_propose(records: list[dict], q: float = 0.99) -> float | None:
    """SUSPECT_EDGE = percentile haut de la NOUVELLE distribution d'EV.

    Détecteur d'erreur de DONNÉES, pas de seuil de rentabilité : au-delà, un
    edge signale un prix mal apparié bien plus souvent qu'une inefficience.
    L'exprimer en percentile plutôt qu'en valeur fixe le rend insensible aux
    changements d'unité — c'est précisément ce qui a manqué le 2026-08-22,
    quand l'edge a changé d'échelle sans que le garde suive.
    """
    vals = [r["edge_replay"] for r in records]
    return round(_pct(vals, q), 2) if vals else None


def plafonds_par_sport(records: list[dict], min_n: int = CALIB_MIN_N) -> dict:
    """Plafond d'EV par sport : plancher de la bande PROUVÉE PERDANTE la plus
    basse au-dessus de la bande qualifiante. Rend {} quand rien n'est prouvé —
    le constat « le football au-dessus de 6 % perd » appartient à l'ANCIENNE
    unité et ne se convertit pas, il se re-mesure."""
    out = {}
    for sport in sorted({r["sport"] for r in records}):
        srec = [r for r in records if r["sport"] == sport]
        bandes = calibration_bands(srec, min_n=min_n)
        perdantes = [b for b in bandes if b["prouvee_perdante"] and b["plancher"] > 0]
        if perdantes:
            out[sport] = {"plafond": perdantes[0]["plancher"],
                          "n": perdantes[0]["n"],
                          "hit_rate": perdantes[0]["hit_rate"],
                          "requis": perdantes[0]["p_breakeven"]}
    return out


# ── Rejeu complet ────────────────────────────────────────────────────────

def replay(rows: list[dict], haircut: float = HAIRCUT_DEFAULT,
           min_edge: float = MIN_EDGE, ev_floor: float = EV_EDGE_FLOOR) -> dict:
    """Rapport complet. Fonction pure de `rows` — aucune I/O, testable."""
    records = [r for r in (normalize(r, haircut) for r in rows) if r]
    decisive = [r for r in records if r["outcome"] in ("WIN", "LOSS", "PUSH")]

    by_sport: dict = {}
    for sport in sorted({r["sport"] for r in records}):
        srows = [r for r in rows if str(r.get("sport") or "") == sport]
        srec  = [r for r in records if r["sport"] == sport]
        sdec  = [r for r in srec if r["outcome"] in ("WIN", "LOSS", "PUSH")]
        by_sport[sport] = {
            "resolution": resolution_rate(srows),
            "n_decisive": len(sdec),
            "edge_stored": distribution([r["edge_stored"] for r in srec]),
            "edge_base":   distribution([r["edge_base"] for r in srec]),
            "edge_replay": distribution([r["edge_replay"] for r in srec]),
            "roi_reel_brut":     roi(sdec, "odds", 0.0),
            "roi_reel_net":      roi(sdec, "odds", TAX_REAL),
            "roi_exec_brut":     roi(sdec, "exec_odds", 0.0),
            "roi_exec_net":      roi(sdec, "exec_odds", TAX_REAL),
            "roi_exec_kelly":    roi(sdec, "exec_odds", TAX_REAL, kelly=True),
            "survivants_min_edge": survivors(srec, min_edge),
            "survivants_ev_floor": survivors(srec, ev_floor),
        }

    by_band: dict = {}
    for lo, hi in EDGE_BANDS:
        label = _band_label(lo, hi)
        brec = [r for r in records if lo <= r["edge_base"] < hi]
        bdec = [r for r in brec if r["outcome"] in ("WIN", "LOSS", "PUSH")]
        by_band[label] = {
            "n": len(brec),
            "n_decisive": len(bdec),
            "edge_replay": distribution([r["edge_replay"] for r in brec]),
            "roi_reel_brut": roi(bdec, "odds", 0.0),
            "roi_exec_net":  roi(bdec, "exec_odds", TAX_REAL),
            "encore_au_dessus_de_min_edge": sum(1 for r in brec if r["edge_replay"] >= min_edge),
        }

    bandes = calibration_bands(records)
    return {
        "calibration": {
            "bandes": bandes,
            "seuil_propose": seuil_propose(bandes),
            "suspect_edge_p99": suspect_edge_propose(records),
            "plafonds_par_sport": plafonds_par_sport(records),
            "min_n": CALIB_MIN_N,
        },
        "parametres": {
            "haircut": haircut, "min_edge": min_edge, "ev_edge_floor": ev_floor,
            "tax_rate_depot": TAX_RATE, "tax_rate_reel": TAX_REAL,
        },
        "n_lignes": len(records),
        "n_decisives": len(decisive),
        "n_devigorisees": sum(1 for r in records if r["devigged"]),
        "periode": {
            "debut": min((r["date"] for r in records if r["date"]), default=""),
            "fin":   max((r["date"] for r in records if r["date"]), default=""),
        },
        "resolution_globale": resolution_rate(rows),
        "unites": {
            "ev": sum(1 for r in records if r["unite_ev"]),
            "ratio_de_prix": sum(1 for r in records if not r["unite_ev"]),
            "tolerance": EDGE_UNIT_TOL,
        },
        "edge_stored": distribution([r["edge_stored"] for r in records]),
        "edge_base":   distribution([r["edge_base"] for r in records]),
        "edge_replay": distribution([r["edge_replay"] for r in records]),
        "roi_reel_brut":  roi(decisive, "odds", 0.0),
        "roi_reel_net":   roi(decisive, "odds", TAX_REAL),
        "roi_exec_brut":  roi(decisive, "exec_odds", 0.0),
        "roi_exec_net":   roi(decisive, "exec_odds", TAX_REAL),
        "roi_exec_kelly": roi(decisive, "exec_odds", TAX_REAL, kelly=True),
        "survivants_min_edge": survivors(records, min_edge),
        "survivants_ev_floor": survivors(records, ev_floor),
        "par_sport": by_sport,
        "par_bande": by_band,
    }


def sensitivity(rows: list[dict], haircuts: list[float]) -> list[dict]:
    """ROI exécutable net et survivants pour plusieurs prélèvements — la
    conclusion doit tenir sur toute la bande mesurée, pas sur un point."""
    out = []
    for h in haircuts:
        rep = replay(rows, haircut=h)
        out.append({
            "haircut": h,
            "edge_base_median": rep["edge_base"].get("median"),
            "edge_replay_median": rep["edge_replay"].get("median"),
            "roi_exec_net": rep["roi_exec_net"]["roi_pct"],
            "survivants_min_edge": rep["survivants_min_edge"]["after"],
            "survivants_ev_floor": rep["survivants_ev_floor"]["after"],
        })
    return out


# ── Lecture (SEULE I/O du script) ────────────────────────────────────────

def fetch_ledger(sb) -> list[dict]:
    """Toutes les lignes d'`ai_learning_ledger`, paginées. LECTURE SEULE."""
    out: list[dict] = []
    start, step = 0, 1000
    while True:
        try:
            res = sb.table("ai_learning_ledger").select("*").range(start, start + step - 1).execute()
        except Exception as e:
            log.error("lecture ai_learning_ledger: %s", e)
            break
        data = res.data or []
        out.extend(data)
        if len(data) < step:
            break
        start += step
    return out


def fetch_signals(sb) -> list[dict]:
    """`signals` — sert au taux de résolution du cycle courant (48h de
    rétention), que le ledger ne voit qu'après purge. LECTURE SEULE."""
    try:
        res = sb.table("signals").select(
            "sport,status,outcome,xbet_odd,edge_pct,sharp_prob,market_key,created_at").execute()
        return res.data or []
    except Exception as e:
        log.error("lecture signals: %s", e)
        return []


# ── Rendu texte ──────────────────────────────────────────────────────────

def _fmt_roi(d: dict) -> str:
    r = d["roi_pct"]
    roi_txt = "—" if r is None else f"{r:+7.2f}%"
    return f"n={d['n']:<4} ROI={roi_txt}  P&L={d['profit']:+8.2f}u"


def _pctd(value) -> str:
    return "—" if value is None else f"{value}%"


def render(rep: dict, sig_resolution: dict | None = None,
           sens: list[dict] | None = None) -> str:
    p = rep["parametres"]
    L = []
    L.append("=" * 78)
    L.append("A0 — REJEU DU LEDGER AU PRIX EXÉCUTABLE (lecture seule)")
    L.append("=" * 78)
    L.append(f"Période      : {rep['periode']['debut']} → {rep['periode']['fin']}")
    L.append(f"Lignes       : {rep['n_lignes']} dont {rep['n_decisives']} décisives "
             f"(WIN/LOSS/PUSH) et {rep['n_devigorisees']} au prix dévigorisé")
    L.append(f"Prélèvement  : h={p['haircut']} (football h2h uniquement ; 1.0 partout ailleurs)")
    L.append(f"Seuils       : MIN_EDGE={p['min_edge']}%  EV_EDGE_FLOOR={p['ev_edge_floor']}%")
    L.append(f"Taxe         : dépôt={p['tax_rate_depot']:.2f}  réelle={p['tax_rate_reel']:.2f}")
    L.append("")

    r = rep["resolution_globale"]
    L.append("── TAUX DE RÉSOLUTION ──────────────────────────────────────────────")
    L.append(f"  ledger  : réglés {r['settled']} / (réglés + expired) {r['denom']} "
             f"= {_pctd(r['rate_pct'])}")
    if sig_resolution:
        s = sig_resolution
        L.append(f"  signals : réglés {s['settled']} / {s['denom']} = {_pctd(s['rate_pct'])}")
    L.append("")

    u = rep["unites"]
    L.append("── UNITÉS D'EDGE PRÉSENTES DANS LE LEDGER ──────────────────────────")
    L.append(f"  unité EV actuelle        : {u['ev']} lignes")
    L.append(f"  ratio de prix (pré-8/22) : {u['ratio_de_prix']} lignes "
             f"— edge écrit non comparable, recalculé pour ce rapport")
    L.append("")

    L.append("── DISTRIBUTION DES EDGES ──────────────────────────────────────────")
    for label, key in (("écrit au scan (2 unités)", "edge_stored"),
                       ("AVANT — prix stocké", "edge_base"),
                       ("APRÈS — prix exécutable", "edge_replay")):
        d = rep[key]
        L.append(f"  {label:<26} n={d['n']:<4} p10={d['p10']:+6.2f}  "
                 f"med={d['median']:+6.2f}  p90={d['p90']:+6.2f}  "
                 f"moy={d['mean']:+6.2f}  [{d['min']:+.2f} … {d['max']:+.2f}]")
    L.append("  (« AVANT » et « APRÈS » emploient la même formule ; seule la cote change.)")
    L.append("")

    L.append("── ROI GLOBAL ──────────────────────────────────────────────────────")
    L.append(f"  réel        brut  : {_fmt_roi(rep['roi_reel_brut'])}")
    L.append(f"  réel        net   : {_fmt_roi(rep['roi_reel_net'])}   (taxe 20 % sur le gain)")
    L.append(f"  exécutable  brut  : {_fmt_roi(rep['roi_exec_brut'])}")
    L.append(f"  exécutable  net   : {_fmt_roi(rep['roi_exec_net'])}")
    L.append(f"  exécutable  Kelly : {_fmt_roi(rep['roi_exec_kelly'])}")
    base = rep["roi_reel_brut"]
    wr = base["win_rate"]
    wr_txt = "—" if wr is None else f"{wr:.2f}%"
    L.append(f"  taux de réussite  : {wr_txt} ({base['wins']} gagnés, "
             f"{base['pushes']} remboursés)")
    L.append("")

    L.append("── COMBIEN DE LIGNES PASSERAIENT ENCORE ────────────────────────────")
    entete = "émis à l'époque"
    L.append(f"  {'seuil':<22} {entete:>16} {'AVANT':>8} {'APRÈS':>8} {'conservées':>12}")
    for key, name in (("survivants_min_edge", "MIN_EDGE"),
                      ("survivants_ev_floor", "EV_EDGE_FLOOR")):
        d = rep[key]
        libelle = f"{name} ({d['floor']}%)"
        L.append(f"  {libelle:<22} {d['emis_a_lepoque']:>16} {d['before']:>8} "
                 f"{d['after']:>8} {_pctd(d['kept_pct']):>12}")
    L.append("")

    L.append("── PAR SPORT ───────────────────────────────────────────────────────")
    for sport, d in rep["par_sport"].items():
        res = d["resolution"]
        eb, er = d["edge_base"], d["edge_replay"]
        s = d["survivants_min_edge"]
        L.append(f"  {sport}")
        L.append(f"    résolution      : {res['settled']}/{res['denom']} = {_pctd(res['rate_pct'])}")
        L.append(f"    edge médian     : {eb['median']:+.2f}%  →  {er['median']:+.2f}%")
        L.append(f"    ROI réel  net   : {_fmt_roi(d['roi_reel_net'])}")
        L.append(f"    ROI exéc. net   : {_fmt_roi(d['roi_exec_net'])}")
        L.append(f"    passe MIN_EDGE  : {s['before']} → {s['after']}")
    L.append("")

    L.append("── PAR BANDE D'EDGE (bande = edge AVANT, unité actuelle) ───────────")
    L.append(f"  {'bande':<12} {'n':>4} {'déc.':>5} {'edge recalc.':>14} "
             f"{'ROI réel brut':>14} {'ROI exéc. net':>14} {'≥MIN_EDGE':>10}")
    for label, d in rep["par_bande"].items():
        if not d["n"]:
            continue
        er = d["edge_replay"].get("median")
        rr = d["roi_reel_brut"]["roi_pct"]
        rx = d["roi_exec_net"]["roi_pct"]
        er_txt = "—" if er is None else f"{er:+.2f}%"
        rr_txt = "—" if rr is None else f"{rr:+.2f}%"
        rx_txt = "—" if rx is None else f"{rx:+.2f}%"
        L.append(f"  {label:<12} {d['n']:>4} {d['n_decisive']:>5} {er_txt:>14} "
                 f"{rr_txt:>14} {rx_txt:>14} "
                 f"{d['encore_au_dessus_de_min_edge']:>10}")
    L.append("")

    if sens:
        L.append("── SENSIBILITÉ AU PRÉLÈVEMENT ──────────────────────────────────────")
        L.append(f"  {'h':>8} {'edge méd.':>12} {'ROI exéc. net':>15} "
                 f"{'≥MIN_EDGE':>11} {'≥EV_FLOOR':>11}")
        for row in sens:
            roi_txt = "—" if row["roi_exec_net"] is None else f"{row['roi_exec_net']:+.2f}%"
            L.append(f"  {row['haircut']:>8.4f} {row['edge_replay_median']:>+11.2f}% "
                     f"{roi_txt:>15} {row['survivants_min_edge']:>11} "
                     f"{row['survivants_ev_floor']:>11}")
        L.append("")

    c = rep["calibration"]
    L.append("── A6 — CALIBRATION PAR BANDE D'EV RECALCULÉE ──────────────────────")
    L.append(f"  Une bande QUALIFIE si : n >= {c['min_n']} réglés, ROI net > 0, "
             f"et Wilson- > point mort.")
    L.append("")
    L.append(f"  {'bande EV':>16} {'n':>4} {'réussite':>9} {'Wilson-':>9} "
             f"{'requis':>8} {'cote moy':>9} {'ROI net':>9}  verdict")
    for b in c["bandes"]:
        if not b["n_total"]:
            continue
        lo = "-inf" if b["plancher"] <= -1e8 else f"{b['plancher']:+.1f}"
        hi = "+inf" if b["plafond"] >= 1e8 else f"{b['plafond']:+.1f}"
        hr = "—" if b["hit_rate"] is None else f"{b['hit_rate'] * 100:.1f}%"
        wl = "—" if b["wilson_lower"] is None else f"{b['wilson_lower'] * 100:.1f}%"
        be = "—" if b["p_breakeven"] is None else f"{b['p_breakeven'] * 100:.1f}%"
        ao = "—" if b["avg_odds"] is None else f"{b['avg_odds']:.2f}"
        ro = "—" if b["roi_net"] is None else f"{b['roi_net'] * 100:+.1f}%"
        if b["qualifie"]:
            verdict = "QUALIFIE"
        elif b["prouvee_perdante"]:
            verdict = "prouvée perdante"
        elif b["n"] < c["min_n"]:
            verdict = f"n<{c['min_n']} — on ne conclut pas"
        else:
            verdict = "non prouvée rentable"
        L.append(f"  {lo + ' → ' + hi:>16} {b['n']:>4} {hr:>9} {wl:>9} "
                 f"{be:>8} {ao:>9} {ro:>9}  {verdict}")
    L.append("")
    seuil = c["seuil_propose"]
    if seuil is None:
        L.append("  ⛔ AUCUNE BANDE NE QUALIFIE.")
        L.append("     Ce n'est pas un échec de mesure : sur ces données, aucun")
        L.append("     niveau d'EV ne se montre rentable de façon prouvée.")
    else:
        L.append(f"  ✅ Bande qualifiante la plus basse → seuil = {seuil:+.1f}%")
    L.append(f"  SUSPECT_EDGE (p99 de la nouvelle distribution) : "
             f"{c['suspect_edge_p99']:+.2f}%")
    pl = c["plafonds_par_sport"]
    if pl:
        for sport, d in pl.items():
            L.append(f"  plafond {sport} : {d['plafond']:+.1f}% "
                     f"(bande prouvée perdante : {d['hit_rate'] * 100:.1f}% "
                     f"pour {d['requis'] * 100:.1f}% requis, n={d['n']})")
    else:
        L.append("  plafonds par sport : aucune bande PROUVÉE perdante — "
                 "rien à poser")
    L.append("")

    L.append("=" * 78)
    L.append("AUCUNE ÉCRITURE EN BASE. Aucun seuil modifié.")
    L.append("=" * 78)
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--haircut", type=float, default=HAIRCUT_DEFAULT,
                    help=f"prélèvement appliqué au football h2h (défaut {HAIRCUT_DEFAULT})")
    ap.add_argument("--haircut-lo", type=float, default=HAIRCUT_P10)
    ap.add_argument("--haircut-hi", type=float, default=HAIRCUT_P90)
    ap.add_argument("--min-edge", type=float, default=MIN_EDGE)
    ap.add_argument("--json", action="store_true", help="sortie JSON brute")
    ap.add_argument("--calibrate-from", metavar="FICHIER",
                    help="JSON de 1X2 soft bruts ([{\"1\":…,\"X\":…,\"2\":…}]) — "
                         "remesure le prélèvement hors-ligne et sort")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s | %(message)s")

    if args.calibrate_from:
        with open(args.calibrate_from, encoding="utf-8") as fh:
            raw = json.load(fh)
        print(json.dumps(measure_haircut(raw), indent=2, ensure_ascii=False))
        return 0

    from core.db import get_db
    sb = get_db(write=False)
    if sb is None:
        log.critical("Supabase non configuré (SUPABASE_URL/SUPABASE_KEY) — rien à lire")
        return 1

    rows = fetch_ledger(sb)
    if not rows:
        log.critical("ai_learning_ledger vide ou illisible")
        return 1

    sig = fetch_signals(sb)
    sig_res = resolution_rate(sig, field="status") if sig else None

    rep = replay(rows, haircut=args.haircut, min_edge=args.min_edge)
    sens = sensitivity(rows, sorted({args.haircut_lo, args.haircut, args.haircut_hi, 1.0}))

    if args.json:
        print(json.dumps({"rapport": rep, "sensibilite": sens,
                          "resolution_signals": sig_res},
                         indent=2, ensure_ascii=False, default=str))
    else:
        print(render(rep, sig_res, sens))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
