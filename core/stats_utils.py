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
    Approximate net-of-tax breakeven win probability for a segment's
    AVERAGE odds: p > 1 / ((1-tax_rate)*avg_odds) — at tax_rate=0.20 this
    is exactly 1.25/avg_odds. Deliberately simple (segment-average odds,
    not each bet's exact price) — a fast, at-a-glance dashboard indicator,
    not a go/no-go gate (core/tax_engine.py's per-signal check with that
    signal's own true probability is the actual gate before any send).

    WARNING — this module is generic statistics with no business-config
    dependency (no import of core.constants) on purpose; the tax_rate=0.20
    default is NOT necessarily the operator's real, currently-configured
    rate (core.constants.TAX_RATE has been 0.0 since 2026-07-08 — see that
    module's docstring). Every caller that gates a real decision (raising/
    lowering a threshold, sizing a stake, displaying "seuil rentable")
    MUST pass tax_rate=constants.TAX_RATE explicitly — see
    core/learning_layer.py's _sport_stats and api/index.py's /performance
    route for the pattern. A bare p_breakeven(avg_odds) call silently
    reintroduces a 20% tax assumption the operator turned off.
    """
    if avg_odds <= 1.0:
        return 1.0
    return round(1 / ((1 - tax_rate) * avg_odds), 4)


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
