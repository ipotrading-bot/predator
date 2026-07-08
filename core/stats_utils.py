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
    AVERAGE odds: p > 1 / ((1-tax_rate)*avg_odds) — at TAX_RATE=0.20 this
    is exactly 1.25/avg_odds. Deliberately simple (segment-average odds,
    not each bet's exact price) — a fast, at-a-glance dashboard indicator,
    not a go/no-go gate (core/tax_engine.py's per-signal check with that
    signal's own true probability is the actual gate before any send).
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
