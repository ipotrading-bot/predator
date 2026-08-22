"""
core/math_engine.py — PAIM v10.4 — Conversion en marché binaire + dévigorisation

Dévigorisation : ENSEMBLE de trois méthodes, médiane puis renormalisation.

POURQUOI UN ENSEMBLE
--------------------
Aucune méthode n'est juste partout, et l'erreur de modèle se paie directement
en edge : sur un favori à 1,02 les trois estimateurs s'écartent de 2,2 points
de probabilité, soit bien plus que le seuil d'émission.
- proportionnelle  p_i = q_i / Σq — SOUS-estime le favori sur les marchés
  asymétriques (biais longshot non corrigé) ;
- puissance        p_i = q_i^e avec Σq_i^e = 1 — SUR-estime le favori dans le
  même régime ;
- Shin (1993)      modèle à paris informés — se place entre les deux.

La médiane des trois retient donc Shin dans le cas normal (vérifié sur les
marchés 2 et 3 voies), tout en restant définie si l'un des deux solveurs
dégénère sur un carnet aberrant — là où une moyenne serait tirée par la
valeur folle. La renormalisation finale est nécessaire : une médiane
composante par composante ne somme pas à 1 par construction.

ATTENTION — l'implémentation précédente (jusqu'au 2026-08-22) prétendait dans
sa docstring faire la méthode puissance mais calculait q_i^(1/Σq) PUIS
renormalisait additivement : ni puissance, ni proportionnelle. Elle
sous-estimait le favori de 2,6 à 3,1 points sous 1,10 (0,1 point à cote égale)
— un biais croissant quand la cote raccourcit, exactement là où le moteur
émettait le plus. Ne pas restaurer ce raccourci.

Doctrine Zero-Draw conservée : football = AH 0.0 uniquement.
"""
import math

# Bornes du solveur puissance : e=1 rend la proportionnelle non corrigée,
# e élevé écrase les longshots. Aucun carnet réel ne sort de [1, 20].
_POWER_EXP_HI = 20.0


def _bisect(f, lo: float, hi: float, tol: float = 1e-12, max_iter: int = 200):
    """Racine de f sur [lo, hi] par bissection, ou None si f n'y change pas
    de signe. Volontairement sans scipy : ce module est appelé des dizaines
    de milliers de fois par scan et ne doit dépendre de rien."""
    f_lo, f_hi = f(lo), f(hi)
    if f_lo == 0.0:
        return lo
    if f_hi == 0.0:
        return hi
    if f_lo * f_hi > 0.0:
        return None
    for _ in range(max_iter):
        mid = 0.5 * (lo + hi)
        if hi - lo < tol:
            return mid
        f_mid = f(mid)
        if f_lo * f_mid <= 0.0:
            hi = mid
        else:
            lo, f_lo = mid, f_mid
    return 0.5 * (lo + hi)


def _devig_multiplicative(qs: list[float]) -> list[float]:
    """Proportionnelle : la marge est retirée au prorata de chaque issue."""
    k = sum(qs)
    return [q / k for q in qs]


def _devig_power(qs: list[float]) -> list[float]:
    """Puissance : e tel que Σ q_i^e = 1. Repli sur la proportionnelle si le
    solveur ne converge pas."""
    e = _bisect(lambda x: sum(q ** x for q in qs) - 1.0, 1.0, _POWER_EXP_HI)
    if e is None:
        return _devig_multiplicative(qs)
    return [q ** e for q in qs]


def _devig_shin(qs: list[float]) -> list[float]:
    """
    Shin (1993) — une fraction z du volume vient de parieurs informés :
        p_i = [√(z² + 4(1−z)·q_i²/Σq) − z] / (2(1−z))
    z est résolu pour que Σp = 1. Repli sur la proportionnelle si besoin.
    """
    k = sum(qs)

    def p_of_z(z: float) -> list[float]:
        denom = 2.0 * (1.0 - z)
        return [(math.sqrt(z * z + 4.0 * (1.0 - z) * q * q / k) - z) / denom
                for q in qs]

    z = _bisect(lambda z: sum(p_of_z(z)) - 1.0, 0.0, 1.0 - 1e-9)
    if z is None:
        return _devig_multiplicative(qs)
    return p_of_z(z)


def _median(values: list[float]) -> float:
    s = sorted(values)
    n = len(s)
    return s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])


def devig(odds: list[float]) -> list[float]:
    """
    Probabilités vraies pour un marché à n issues, ensemble des trois
    méthodes. Rend une liste de 0.0 si une cote est invalide (<= 1.01) —
    même contrat de dégradation que le reste du moteur.
    """
    if not odds or any((o is None or o <= 1.01) for o in odds):
        return [0.0] * len(odds)
    qs = [1.0 / o for o in odds]
    if sum(qs) <= 0.0:
        return [0.0] * len(odds)
    candidates = [_devig_multiplicative(qs), _devig_power(qs), _devig_shin(qs)]
    merged = [_median([c[i] for c in candidates]) for i in range(len(qs))]
    total = sum(merged)
    if total <= 0.0:
        return [0.0] * len(odds)
    return [p / total for p in merged]


def devig_bounds(own_odd: float, other_odd: float) -> tuple[float, float]:
    """
    (p_mediane, p_conservatrice) du côté `own_odd` dans un marché binaire.

    p_mediane       — l'estimateur d'ensemble de `devig()` : sert au calcul
                      d'edge et à la mise (Kelly).
    p_conservatrice — le MINIMUM des trois méthodes, non renormalisé : c'est
                      la pratique « worst case » des outils professionnels
                      (n'émettre que si l'EV reste positif sous la méthode la
                      plus défavorable). Sur un carnet déjà sans marge, les
                      trois méthodes coïncident et la borne égale la médiane.

    Rend (0.0, 0.0) sur entrée invalide — même contrat que devig_prob().
    """
    if own_odd is None or other_odd is None or own_odd <= 1.01 or other_odd <= 1.01:
        return 0.0, 0.0
    qs = [1.0 / own_odd, 1.0 / other_odd]
    if sum(qs) <= 0.0:
        return 0.0, 0.0
    candidates = [_devig_multiplicative(qs), _devig_power(qs), _devig_shin(qs)]
    merged = [_median([c[i] for c in candidates]) for i in range(2)]
    total = sum(merged)
    if total <= 0.0:
        return 0.0, 0.0
    p_med = merged[0] / total
    p_cons = min(c[0] for c in candidates)
    return round(p_med, 4), round(p_cons, 4)


def _power_devig3(o1: float, o2: float, o3: float) -> tuple[float, float, float]:
    """
    Dévigorisation d'un marché 3 voies. Nom conservé pour l'historique ;
    l'estimateur est désormais l'ensemble de `devig()`.
    Rend (0,0,0) sur entrée invalide.
    """
    p = devig([o1, o2, o3])
    return p[0], p[1], p[2]


def calc_dnb(odd_team: float, odd_other: float, odd_draw: float) -> float:
    """
    AH 0.0 / Draw No Bet, dévigorisé par l'ensemble de `devig()`.

    En dévigorisation additive, odd_draw se simplifie algébriquement
    (DNB = 1 + o_team/o_other) : le nul n'influence plus rien. Les trois
    estimateurs de l'ensemble le conservent, ce qui compte sur les marchés
    asymétriques où l'additif se trompe jusqu'à 1,5 % sur un gros favori.

    Rend 0.0 sur entrée invalide.
    """
    pt, po, _ = _power_devig3(odd_team, odd_other, odd_draw)
    if pt <= 0.0 or po <= 0.0:
        return 0.0
    denom = pt + po
    if denom <= 0.0:
        return 0.0
    return round(denom / pt, 4)


def devig_prob(own_odd: float, other_odd: float) -> float:
    """
    Probabilité vraie du côté `own_odd` dans un marché BINAIRE (spreads,
    totals, ML tennis/NBA, DNB contre DNB), dévigorisée par l'ensemble des
    trois méthodes — voir la docstring du module.

    Rend 0.0 si l'une des deux cotes est invalide.
    """
    return devig_bounds(own_odd, other_odd)[0]


def is_round_number_line(point: float) -> bool:
    """True if a totals line is a whole number (e.g. 8.0, 9.0) — these can
    push. Half-lines (.5) never push — P(push)=0, no adjustment needed."""
    return bool(point) and point > 0 and point % 1 == 0


def to_binary(odds: dict, sport: str, home: str = "", away: str = "") -> tuple[float, str | None, str]:
    """
    Convert raw 1N2 odds to a binary market price.
    Returns (best_odd, market_label, favorite_team_name).

    Soccer: MUST produce AH 0.0. No draw odd → (0.0, None, "") → REJECT.
    Tennis / Basketball: Moneyline (naturally binary).
    """
    o1 = float(odds.get("1") or 0)
    ox = float(odds.get("X") or 0)
    o2 = float(odds.get("2") or 0)

    if sport == "soccer":
        if ox <= 1.01 or o1 <= 1.01 or o2 <= 1.01:
            return 0.0, None, ""
        dnb_home = calc_dnb(o1, o2, ox)
        dnb_away = calc_dnb(o2, o1, ox)
        # Use RAW 1X2 odds to pick the favourite — DNB formula can fail
        # for extreme edge-cases, but raw odds never lie.
        if o1 <= o2:
            return (dnb_home, "AH 0.0", home) if dnb_home > 1.01 else (0.0, None, "")
        else:
            return (dnb_away, "AH 0.0", away) if dnb_away > 1.01 else (0.0, None, "")

    # Tennis / Basketball — no draw market
    if o1 > 1.01 and (o2 <= 1.01 or o1 <= o2):
        return o1, "Moneyline", home
    elif o2 > 1.01:
        return o2, "Moneyline", away
    return 0.0, "Moneyline", ""
