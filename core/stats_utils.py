"""
core/stats_utils.py — PAIM v9.5 — Statistical rigor helpers for the
dashboard (Task 4). Wilson score interval for win-rate confidence,
tax-adjusted breakeven probability, and Brier score / calibration
bucketing — a raw win rate on its own hides both small-sample noise and
miscalibration, which is exactly what this module surfaces instead.
"""
import math

Z_95 = 1.959963984540054   # 95% two-sided normal quantile


def wilson_ci(wins: int, n: int, z: float = Z_95) -> tuple[float, float]:
    """
    Wilson score interval for a binomial win rate (95% by default) — the
    correct interval at small/moderate sample sizes, unlike the naive
    normal approximation (p_hat ± z*sqrt(p_hat*(1-p_hat)/n)), which can
    overshoot past [0, 1] and is overconfident exactly when n is small,
    i.e. exactly when this matters most. Returns (lower, upper) in [0, 1].
    """
    if n <= 0:
        return (0.0, 1.0)
    p_hat = wins / n
    denom = 1 + z ** 2 / n
    center = p_hat + z ** 2 / (2 * n)
    margin = z * math.sqrt(p_hat * (1 - p_hat) / n + z ** 2 / (4 * n ** 2))
    lower = (center - margin) / denom
    upper = (center + margin) / denom
    return (max(0.0, lower), min(1.0, upper))


def p_breakeven(avg_odds: float, tax_rate: float = 0.20) -> float:
    """
    Taux de réussite minimal, net de taxe, pour une cote MOYENNE de segment :

        p > 1 / (1 + (cote − 1)·(1 − taux))

    Déduction : à l'équilibre, p·(cote−1)·(1−taux) = (1−p) — le gain net
    espéré d'un pari gagnant compense exactement la mise perdue le reste du
    temps. C'est le modèle de taxe du dépôt : la retenue frappe le GAIN NET
    d'un pari gagnant, jamais la mise ni le payout brut (cf. la docstring de
    core/tax_engine.py et `core.constants.net_b`).

    ⚠️ CORRIGÉ LE 2026-08-27. La formule précédente était
    `1 / ((1−taux)·cote)`, qui suppose la taxe prélevée sur le PAYOUT BRUT —
    un modèle que ce dépôt n'applique nulle part ailleurs. Elle surestimait
    le taux requis, d'autant plus que la cote est courte :

        cote 1,35 → 92,6 % exigés au lieu de 78,1 %  (+14,5 points)
        cote 1,30 → 96,2 % au lieu de 80,6 %         (+15,6 points)
        cote 2,50 → 62,5 % au lieu de 45,5 %         (+17,0 points)

    Le défaut était DORMANT tant que `core.constants.TAX_RATE` valait 0.0 :
    à taux nul les deux formules donnent 1/cote, à la décimale près. Le
    rétablissement du taux réel (0.20) le 2026-08-27 l'a réveillé, et il
    s'est vu tout de suite — la couche d'apprentissage déclarait « prouvée
    perdante » la bande de cotes 1,35 du 2026-08-02, dont le taux de réussite
    mesuré (82,4 %) rapporte en réalité +5,5 % d'EV nette. Poser un plafond
    de cote là-dessus aurait coupé l'essentiel du volume sur une erreur
    d'algèbre.

    Volontairement simple (cote moyenne du segment, pas le prix exact de
    chaque pari) — indicateur de lecture rapide pour le dashboard, pas un
    gate d'émission (le juge réel reste core/tax_engine.py, par signal et
    avec sa probabilité propre).

    WARNING — this module is generic statistics with no business-config
    dependency (no import of core.constants) on purpose: the tax_rate=0.20
    default is NOT guaranteed to match the operator's configured rate. It
    happens to match it since 2026-08-27 (core.constants.TAX_RATE was
    restored to 0.20 that day, after having been 0.0 from 2026-07-08 — see
    that module's docstring), and that coincidence is exactly what makes a
    bare call dangerous: it works today and lies silently the day the rate
    moves. Every caller that gates a real decision (raising/lowering a
    threshold, sizing a stake, displaying "seuil rentable") MUST pass
    tax_rate=constants.TAX_RATE explicitly — see core/learning_layer.py's
    _sport_stats and api/index.py's /performance route for the pattern.
    """
    if avg_odds <= 1.0:
        return 1.0
    return round(1 / (1 + (avg_odds - 1) * (1 - tax_rate)), 4)


def brier_score(predictions: list[tuple[float, int]]) -> float | None:
    """
    Mean Brier score over (predicted_prob, outcome) pairs, outcome in
    {0, 1}. 0 = perfect, 0.25 = an uninformative coin-flip forecast,
    1 = perfectly wrong. Win rate alone can't detect miscalibration — a
    model claiming "70% confident" that only wins 50% of the time can
    still show a healthy-looking aggregate win rate if it's mixed with
    genuinely strong 90%+ picks; Brier score by confidence bucket (see
    bucket_predictions below) is what catches that.
    """
    if not predictions:
        return None
    return round(sum((p - o) ** 2 for p, o in predictions) / len(predictions), 4)


def brier_reference(predictions: list[tuple[float, int]]) -> float | None:
    """Brier qu'obtiendrait un modèle PARFAITEMENT calibré sur ces mêmes
    probabilités annoncées — c'est-à-dire la moyenne de p(1-p).

    Sans cette référence, le score brut n'est pas interprétable : le Brier a un
    plancher irréductible qui dépend de la difficulté des paris. Un portefeuille
    de quasi pile-ou-face ne peut PAS descendre sous ~0,25 même en étant
    parfaitement calibré (p=0,50 → 0,2500 ; p=0,55 → 0,2475 ; p=0,60 → 0,2400),
    alors qu'un portefeuille de gros favoris descend naturellement plus bas
    (p=0,70 → 0,2100). Comparer un Brier à une constante revient donc à juger
    la difficulté du marché, pas la qualité du modèle.

    Mesuré le 2026-08-06 : le seuil dur de 0,23 qu'utilisait
    core/learning_layer.py déclarait surconfiants TOUS les sports, y compris le
    football (0,2319 pour une référence de 0,2365) et le basket (0,2304 pour
    0,2463) qui font mieux que leur propre référence. Il forçait donc une
    hausse de plancher à chaque audit — second cliquet, indépendant de celui
    des 60%/82%.
    """
    if not predictions:
        return None
    return round(sum(p * (1 - p) for p, _ in predictions) / len(predictions), 4)


BRIER_BUCKETS = [(0.50, 0.60), (0.60, 0.70), (0.70, 0.80), (0.80, 1.01)]


def bucket_predictions(predictions: list[tuple[float, int]]) -> dict:
    """
    Group (predicted_prob, outcome) pairs into probability buckets
    (50-60%, 60-70%, 70-80%, 80%+) and compute each bucket's sample size,
    observed win rate, average predicted probability, and Brier score — a
    simple reliability diagram. A well-calibrated bucket's observed win
    rate should track its average predicted probability; a persistent gap
    flags over/under-confidence localized to that probability range,
    which an aggregate win rate or a single overall Brier score both hide.
    """
    buckets: dict = {}
    for lo, hi in BRIER_BUCKETS:
        label = f"{int(lo * 100)}-{int(min(hi, 1.0) * 100)}%"
        in_bucket = [(p, o) for p, o in predictions if lo <= p < hi]
        if not in_bucket:
            buckets[label] = {"n": 0, "win_rate": None, "brier": None, "avg_predicted": None}
            continue
        n = len(in_bucket)
        wins = sum(o for _, o in in_bucket)
        buckets[label] = {
            "n": n,
            "win_rate": round(wins / n, 4),
            "brier": brier_score(in_bucket),
            "avg_predicted": round(sum(p for p, _ in in_bucket) / n, 4),
        }
    return buckets
