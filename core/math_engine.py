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

RÉPARTITION DES RÔLES (2026-08-27) — la dévigorisation sert à ESTIMER une
probabilité, jamais à fixer un prix d'entrée. `calc_dnb` est donc réservé au
côté SHARP ; le côté SOFT passe par `synthetic_dnb`, qui rend la cote
réellement exécutable. Confondre les deux fait mesurer une divergence
d'opinion et l'appeler « edge ».
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


def synthetic_dnb(team_odd: float, draw_odd: float) -> float:
    """
    Cote RÉELLEMENT EXÉCUTABLE d'un Draw No Bet synthétique, construit chez UN
    SEUL book à partir de son 1X2 brut :

        exec = o_équipe · (o_nul − 1) / o_nul

    Démonstration en une ligne : pour une mise totale de 1, on place 1/o_nul
    sur le nul (qui rend alors exactement 1, donc la mise) et le reste,
    1 − 1/o_nul, sur l'équipe. Un nul rembourse ; une victoire rapporte
    (1 − 1/o_nul) · o_équipe, ce qui est la formule ci-dessus.

    ⚠️ NE PAS CONFONDRE AVEC `calc_dnb`. `calc_dnb` DÉVIGORISE : il rend le
    prix qu'un book sans marge afficherait, c'est-à-dire une ESTIMATION DE
    PROBABILITÉ. Il n'est légitime que du côté SHARP, où l'on cherche
    justement à estimer la probabilité vraie. L'employer du côté SOFT — ce que
    faisait `to_binary` jusqu'au 2026-08-27 — revient à comparer une opinion à
    une opinion et à appeler « edge » l'écart : le coût de transaction n'est
    jamais soustrait. Mesuré le 2026-08-27 sur les 1X2 bruts du jour, le prix
    exécutable vaut 0,87 à 0,98 fois le prix dévigorisé (médiane 0,89 chez
    1xbet, 0,92 sur le panel api-sports) — de 2 à 13 points d'EV, quand le
    moteur émettait à partir de 1,2 %.

    Rend 0.0 sur entrée invalide — même contrat de dégradation que le reste
    du module.
    """
    if not team_odd or not draw_odd or team_odd <= 1.01 or draw_odd <= 1.01:
        return 0.0
    price = team_odd * (draw_odd - 1.0) / draw_odd
    return round(price, 4) if price > 1.0 else 0.0


def dnb_leg_split(draw_odd: float) -> tuple[float, float]:
    """
    (part_sur_le_nul, part_sur_l_équipe) d'une mise TOTALE de 1 sur un DNB
    synthétique. Somme = 1 par construction.

    Cette répartition n'est pas un détail d'affichage : un DNB synthétique
    engage DEUX paris chez le même book, et l'opérateur qui miserait la mise
    conseillée entièrement sur l'équipe prendrait une exposition au nul que le
    calcul d'EV ne modélise pas. Rend (0.0, 0.0) sur entrée invalide.
    """
    if not draw_odd or draw_odd <= 1.01:
        return 0.0, 0.0
    part_nul = 1.0 / draw_odd
    return round(part_nul, 4), round(1.0 - part_nul, 4)


def executable_price(odds: dict, sport: str, side: str) -> float:
    """
    Prix SOFT EXÉCUTABLE d'un côté donné (`side` = "1" pour le domicile,
    "2" pour l'extérieur), à partir des cotes brutes d'UN book.

    Football : AH 0.0 brut si la source l'expose (`ah0_1`/`ah0_2`), sinon DNB
    synthétique sur le 1X2 de ce book. Hors football : la cote brute du côté.
    Rend 0.0 quand rien n'est jouable — jamais de repli sur une autre cote.

    Point unique de la règle : `to_binary` (prix d'entrée) et le repricing de
    dernière minute de `run_engine` s'en servent tous les deux. Les laisser
    calculer chacun leur prix ferait comparer, au last-look, une cote 1X2 brute
    à un DNB synthétique — la marge du book passerait alors pour un mouvement
    de ligne, sans qu'aucune erreur ne soit levée.
    """
    if side not in ("1", "2"):
        return 0.0
    own = float(odds.get(side) or 0)
    if own <= 1.01:
        return 0.0
    if sport != "soccer":
        return round(own, 4)
    ah0 = float(odds.get("ah0_1" if side == "1" else "ah0_2") or 0)
    if ah0 > 1.01:
        return round(ah0, 4)
    return synthetic_dnb(own, float(odds.get("X") or 0))


def to_binary(odds: dict, sport: str, home: str = "", away: str = "") -> tuple[float, str | None, str]:
    """
    Prix SOFT EXÉCUTABLE d'un marché binaire, à partir des cotes brutes.
    Rend (cote_exécutable, libellé_de_marché, nom_du_favori).

    Football — AH 0.0 obligatoire (doctrine Zero-Draw), et le prix rendu est
    celui qu'on peut RÉELLEMENT jouer :
      1. si la source expose un vrai AH 0.0 (clés `ah0_1` / `ah0_2`), on prend
         sa cote BRUTE — c'est un marché que le book affiche, rien à
         reconstruire ;
      2. sinon on construit le DNB synthétique `synthetic_dnb()` sur le 1X2
         brut du book.
    Sans cote de nul, ni l'un ni l'autre n'est possible → (0.0, None, "") →
    REFUS. Jamais de repli sur le moneyline : comparer une entrée ML à une
    référence DNB donne un edge faux et silencieux.

    Tennis / Basket / MMA — moneyline, naturellement binaire, cote brute.

    ⚠️ CE QUI A CHANGÉ LE 2026-08-27, ET POURQUOI ON NE REVIENT PAS EN ARRIÈRE
    -------------------------------------------------------------------------
    Cette fonction rendait `calc_dnb(...)`, c'est-à-dire le DNB DÉVIGORISÉ.
    Un tel prix n'est affiché par aucun book : la marge en avait été retirée.
    L'« edge » publié mesurait donc la divergence d'opinion entre le book soft
    et la référence sharp, sans jamais soustraire le coût de transaction.
    `calc_dnb` reste employé — mais du côté SHARP UNIQUEMENT, où dévigoriser
    est exactement ce qu'on veut faire (estimer une probabilité). Voir la
    docstring de `synthetic_dnb` pour l'ordre de grandeur mesuré.

    ⚠️ LE PRIX RENDU ENGAGE DEUX JAMBES CHEZ LE MÊME BOOK
    ------------------------------------------------------
    Quand le DNB est SYNTHÉTIQUE (cas 2, le cas courant), la cote rendue n'est
    jouable qu'en plaçant simultanément deux paris : `dnb_leg_split(o_nul)`
    donne la répartition. Conséquences que l'appelant DOIT porter :
      · `kelly_pct` calculé sur cette cote est l'exposition TOTALE, à répartir
        entre les deux jambes — ce n'est pas une mise à poser sur l'équipe ;
      · `advice` doit énoncer la répartition, sinon l'opérateur mise tout sur
        l'équipe et détient une exposition au nul que l'EV n'a pas modélisée ;
      · les deux jambes doivent partir chez LE MÊME book, sinon la cote n'est
        pas celle qui a été calculée (voir `core.api_sports.extract_prices` :
        le line shopping se fait sur le prix final, jamais par issue).
    Dans le cas 1 (AH 0.0 réel) il n'y a qu'une jambe et `dnb_leg_split` n'a
    pas lieu d'être — le libellé rendu distingue les deux situations.
    """
    o1 = float(odds.get("1") or 0)
    o2 = float(odds.get("2") or 0)

    if sport == "soccer":
        # Le favori se choisit sur le 1X2 BRUT : une cote brute ne ment
        # jamais, là où une formule peut dégénérer sur un carnet aberrant.
        if o1 <= 1.01 or o2 <= 1.01:
            return 0.0, None, ""
        fav_is_home = o1 <= o2
        fav_name = home if fav_is_home else away

        price = executable_price(odds, sport, "1" if fav_is_home else "2")
        if price <= 1.01:
            return 0.0, None, ""
        # Une jambe si le book cote un vrai AH 0.0, deux s'il faut le
        # construire — le libellé doit le dire, c'est ce que l'opérateur lit.
        une_jambe = float(odds.get("ah0_1" if fav_is_home else "ah0_2") or 0) > 1.01
        return price, "AH 0.0" if une_jambe else "AH 0.0 (2 jambes)", fav_name

    # Tennis / Basketball / MMA — no draw market, the raw price is executable.
    if o1 > 1.01 and (o2 <= 1.01 or o1 <= o2):
        return round(o1, 4), "Moneyline", home
    elif o2 > 1.01:
        return round(o2, 4), "Moneyline", away
    return 0.0, "Moneyline", ""
