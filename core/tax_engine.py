"""
core/tax_engine.py — PAIM v9.5 — Tax-aware EV, Kelly staking, system viability.

AUTHORED FROM THE TASK DESCRIPTION ONLY. The tax_engine.py this module was
meant to be copied from, and both spec documents referenced alongside it
(predator-tax-upgrade-spec.md and one other), are not present anywhere in
this repo — checked the working tree, `git log --all`, and `git stash list`.
This is a from-scratch implementation of the described API surface
(system_expected_value, suggest_system, optimal_stake_fraction,
is_combo_tax_viable, min_edge_required). Treat every formula below as
something to review, not as ground truth transcribed from an existing
document — confirm the tax model assumption below matches your actual
bookmaker/jurisdiction rule before trusting it with real stakes.

── Tax model assumed ───────────────────────────────────────────────────
TAX_RATE is withheld on the NET PROFIT of a WINNING bet only:
    net_return = stake + stake * (odds - 1) * (1 - TAX_RATE)
A losing bet is not taxed (you simply lose the stake). A push/void returns
the stake untaxed. This is the common "tax on winnings" structure. If your
bookmaker instead taxes the gross payout (stake * odds) or the stake at
placement, `net_b()` is the only function that needs to change — every
other function composes from it.

── "System" here means an accumulator/parlay ───────────────────────────
suggest_system() combines legs into a single accumulator (all legs must
win), not a formal partial-coverage "système X/Y" bet (which pays out on
any k of N legs hitting and splits the stake across C(N,k)
sub-combinations — a materially different, more complex payout structure).
The task's own wording — "grouper... au lieu d'alerter chaque signal
isolément" — describes bundling signals that fire in the same window into
one bet slip, which is exactly what an accumulator does. If true
partial-system coverage is what actually gets placed, this module needs a
different payout function; flag it and it can be added.

── Independence assumption ─────────────────────────────────────────────
Every combined-probability calculation below multiplies each leg's
true_prob independently. Two legs from the same match/league/kickoff
window are NOT independent in reality — see core/tax_engine.py's
correlation extension (correlation_group discount, added for Task 5) for
the correlated case. Functions here should only be called directly with
legs already confirmed independent, or with a pre-discounted combined_p.
"""
import logging
import math
from itertools import combinations

from scipy.optimize import minimize_scalar

log = logging.getLogger("PREDATOR.tax_engine")

DEFAULT_TAX_RATE = 0.20
# Reference true probability used by min_edge_required() when no specific
# leg is known yet (e.g. sizing a floor threshold before signals exist).
# 0.55 matches SHARP_PROB_MIN in core/paim_engine.py — the codebase's
# existing "typical sharp signal" reference point, reused here rather than
# inventing a new arbitrary constant.
DEFAULT_REFERENCE_PROB = 0.55
MAX_SYSTEM_LEGS = 4   # compounding tax + variance beyond this is too aggressive for a soft-book audience


def net_b(odds: float, tax_rate: float = DEFAULT_TAX_RATE) -> float:
    """Tax-adjusted Kelly 'b' — net profit multiplier per unit staked on a win."""
    return (odds - 1) * (1 - tax_rate)


def net_return_on_win(stake: float, odds: float, tax_rate: float = DEFAULT_TAX_RATE) -> float:
    """Total amount returned to the bettor on a winning bet, net of tax on the profit portion."""
    return stake + stake * net_b(odds, tax_rate)


def single_bet_ev_net_tax(stake: float, odds: float, true_prob: float,
                          tax_rate: float = DEFAULT_TAX_RATE) -> float:
    """Expected value in currency units, net of tax, for one bet (or one
    already-combined accumulator treated as a single price/probability)."""
    if stake <= 0 or odds <= 1.0 or not (0 <= true_prob <= 1):
        return 0.0
    b = net_b(odds, tax_rate)
    return stake * (true_prob * b - (1 - true_prob))


def min_edge_required(k: int = 1, true_prob: float = DEFAULT_REFERENCE_PROB,
                       tax_rate: float = DEFAULT_TAX_RATE) -> float:
    """
    Minimum PER-LEG edge (%) required for a k-leg accumulator, at uniform
    per-leg true probability `true_prob`, to break even net of tax.

    Derivation: a bet is net-+EV iff
        true_prob * (odds - 1) * (1 - tax_rate) > (1 - true_prob)
    Writing odds = fair_odds * (1 + e), fair_odds = 1/true_prob, e = edge:
        e > tax_rate * (1 - true_prob) / (1 - tax_rate)                 [k = 1]
    For a k-leg accumulator (combined prob P = true_prob**k, combined odds
    O = (fair_odds*(1+e))**k), solving the same breakeven condition for the
    combo's compounded edge (1+e)**k - 1 gives:
        e > [1 + tax_rate*(1-P)/(1-tax_rate)] ** (1/k)  -  1
    which collapses to the k=1 formula above when k=1. Increasing k raises
    the required per-leg edge — tax compounds on the combined payout faster
    than a flat sum of per-leg edges can outrun it.

    NOTE: assumes every leg shares the same true_prob — a simplification to
    get a single scalar "how much edge is needed at this k" floor. Real
    combos mix legs of different true_prob; use is_combo_tax_viable() with
    the actual legs for the final go/no-go check before sending.
    """
    if k < 1:
        raise ValueError("k must be >= 1")
    if not (0 < true_prob < 1):
        raise ValueError("true_prob must be in (0, 1)")
    if not (0 <= tax_rate < 1):
        raise ValueError("tax_rate must be in [0, 1)")
    combined_p = true_prob ** k
    threshold = 1 + tax_rate * (1 - combined_p) / (1 - tax_rate)
    compounded_edge = threshold ** (1 / k) - 1
    return round(compounded_edge * 100, 4)


def _safe_log(x: float) -> float:
    return math.log(x) if x > 1e-12 else -1e12


def optimal_stake_fraction(true_prob: float, odds: float,
                           tax_rate: float = DEFAULT_TAX_RATE,
                           kelly_multiplier: float = 1.0) -> float:
    """
    Fraction of bankroll to stake, maximizing expected log-growth (Kelly
    criterion) on the tax-adjusted payout, found by bounded numerical
    optimization (scipy.optimize.minimize_scalar, method="bounded") rather
    than a fixed-step grid search — a coarse grid systematically misses the
    optimum precisely in the near-zero-EV regime that a tax haircut pushes
    marginal signals into.

    `kelly_multiplier` applies a fractional-Kelly scale-down (see
    KELLY_FRACTION in core/constants.py) on top of the numerically-found
    full-Kelly fraction.
    """
    if true_prob <= 0 or true_prob >= 1 or odds <= 1.0:
        return 0.0
    b = net_b(odds, tax_rate)
    if b <= 0:
        return 0.0

    def neg_log_growth(f):
        win_term = true_prob * _safe_log(1 + f * b)
        loss_term = (1 - true_prob) * _safe_log(1 - f)
        return -(win_term + loss_term)

    result = minimize_scalar(neg_log_growth, bounds=(0.0, 0.999), method="bounded")
    full_kelly = result.x if result.success else 0.0
    # The bounded optimizer settles near, but not exactly at, the f=0
    # boundary for a negative-edge bet (solver tolerance, not a real
    # signal) — clamp anything below a stake-sized epsilon to a clean
    # zero rather than leaking a dust-sized "optimal" stake.
    if full_kelly < 1e-4:
        return 0.0
    return round(max(0.0, full_kelly * kelly_multiplier), 6)


def _combine(legs: list[dict]) -> tuple[float, float]:
    """Combined (probability, odds) for independent legs — see module
    docstring's independence assumption."""
    combined_p = 1.0
    combined_odds = 1.0
    for leg in legs:
        combined_p *= leg["true_prob"]
        combined_odds *= leg["odds"]
    return combined_p, combined_odds


# ── Correlation between legs (Task 5) ───────────────────────────────────
# Two legs sharing a correlation_group (see core/paim_engine.correlation_group
# — same sport/kickoff-date/league, which always includes "two markets on
# the same match") are NOT independent, so multiplying their true_probs
# directly overstates the combo's real win probability.
DEFAULT_CORRELATION_MODE = "forbid"   # "forbid" | "discount"
DEFAULT_CORRELATION_RHO = 0.20        # 0.15-0.30 requested range; 0.20 as a
                                      # moderate midpoint default — same-day
                                      # same-league legs share some common
                                      # factors (ref pool, weather, travel)
                                      # but are far from perfectly correlated.


def _pairwise_gaussian_copula_joint(p_a: float, p_b: float, rho: float) -> float:
    """
    Joint probability of two binary events under a Gaussian copula with
    correlation `rho` between their latent standard-normal variables:
    threshold_x = Phi^-1(p_x) (probit of each marginal), joint = the
    bivariate normal CDF at those thresholds. rho=0 recovers independence
    (p_a * p_b) exactly.

    IMPORTANT sign convention: `rho` here is passed to the copula as
    NEGATIVE internally (see _combine_with_correlation), because a
    Gaussian copula with rho >= 0 satisfies positive quadrant dependence
    for ANY marginals — P(A and B) >= P(A)*P(B) always holds, it can only
    ever push the joint probability UP, never down. A configured
    `rho` in [0.15, 0.30] meant to express "these two legs are not safely
    independent, be more conservative" therefore has to enter this formula
    as -rho to produce an actual reduction ("discount") in the joint
    probability estimate — passing it as positive would make correlated
    combos look MORE viable than treating them as independent, the
    opposite of this feature's purpose.
    """
    if rho == 0:
        return p_a * p_b
    from scipy.stats import multivariate_normal, norm
    za, zb = norm.ppf(p_a), norm.ppf(p_b)
    return float(multivariate_normal(mean=[0.0, 0.0], cov=[[1.0, rho], [rho, 1.0]]).cdf([za, zb]))


def _combine_with_correlation(legs: list[dict], correlation_mode: str = DEFAULT_CORRELATION_MODE,
                              rho: float = DEFAULT_CORRELATION_RHO) -> tuple[float, float, bool]:
    """
    Combined (probability, odds, ok) for `legs`, accounting for
    correlation_group tags. ok=False means the caller must discard this
    combo outright (only possible in "forbid" mode with a shared group).

    "forbid" (default): refuses to combine two legs sharing a
      correlation_group — the safe default when there's no verified
      correlation estimate for this specific pairing; a wrong rho is worse
      than not combining at all for a system meant to be mathematically
      honest rather than merely optimistic.
    "discount": instead of refusing, shrinks the naive independent
      product for every pair sharing a group via a pairwise Gaussian
      copula (see _pairwise_gaussian_copula_joint) evaluated at -rho —
      `rho` (0.15-0.30) is a configured "how non-independent are these"
      magnitude, and the copula needs the negative sign to actually
      reduce the joint probability estimate rather than inflate it (see
      _pairwise_gaussian_copula_joint's docstring for the PQD reasoning).
      For more than one correlated pair sharing the same group this is an
      approximation — each pair is corrected independently rather than
      solved as a single n-dimensional copula — documented here rather
      than silently assumed exact.
    """
    combined_p, combined_odds = _combine(legs)

    groups = [leg.get("correlation_group") for leg in legs if leg.get("correlation_group")]
    has_shared_group = len(groups) != len(set(groups))
    if not has_shared_group:
        return combined_p, combined_odds, True
    if correlation_mode == "forbid":
        return combined_p, combined_odds, False

    from collections import defaultdict
    by_group: dict = defaultdict(list)
    for leg in legs:
        g = leg.get("correlation_group")
        if g:
            by_group[g].append(leg)

    for _group, group_legs in by_group.items():
        if len(group_legs) < 2:
            continue
        for a, b in combinations(group_legs, 2):
            independent_joint = a["true_prob"] * b["true_prob"]
            if independent_joint <= 0:
                continue
            correlated_joint = _pairwise_gaussian_copula_joint(a["true_prob"], b["true_prob"], -rho)
            combined_p *= correlated_joint / independent_joint

    return max(0.0, min(1.0, combined_p)), combined_odds, True


def system_expected_value(legs: list[dict], stake: float,
                          tax_rate: float = DEFAULT_TAX_RATE,
                          correlation_mode: str = DEFAULT_CORRELATION_MODE,
                          rho: float = DEFAULT_CORRELATION_RHO) -> dict:
    """
    Combined probability/odds/EV for an accumulator formed from `legs`
    (each a dict with 'true_prob' and 'odds', optionally 'correlation_group').
    Works for a single leg too (a list of length 1) — the "single-bet
    equivalent" the task refers to.
    """
    if not legs:
        return {"combined_prob": 0.0, "combined_odds": 0.0, "ev": 0.0, "viable": False}
    combined_p, combined_odds, ok = _combine_with_correlation(legs, correlation_mode, rho)
    if not ok:
        return {"combined_prob": combined_p, "combined_odds": combined_odds, "ev": 0.0, "viable": False}
    ev = single_bet_ev_net_tax(stake, combined_odds, combined_p, tax_rate)
    return {
        "combined_prob": combined_p,
        "combined_odds": combined_odds,
        "ev": ev,
        "viable": ev > 0,
    }


def is_combo_tax_viable(legs: list[dict], tax_rate: float = DEFAULT_TAX_RATE,
                        correlation_mode: str = DEFAULT_CORRELATION_MODE,
                        rho: float = DEFAULT_CORRELATION_RHO) -> bool:
    """
    True if the combo formed by ALL given legs is net-+EV after tax, using
    each leg's own true probability and odds — the real go/no-go check on
    the actual combo about to be sent (unlike min_edge_required()'s
    uniform-true_prob reference threshold). False whenever two legs share
    a correlation_group and correlation_mode="forbid" (the default).
    """
    if not legs:
        return False
    combined_p, combined_odds, ok = _combine_with_correlation(legs, correlation_mode, rho)
    if not ok:
        return False
    return single_bet_ev_net_tax(1.0, combined_odds, combined_p, tax_rate) > 0


def suggest_system(signals: list[dict], bankroll: float,
                   tax_rate: float = DEFAULT_TAX_RATE,
                   kelly_multiplier: float = 1.0,
                   max_legs: int = MAX_SYSTEM_LEGS,
                   correlation_mode: str = DEFAULT_CORRELATION_MODE,
                   rho: float = DEFAULT_CORRELATION_RHO) -> dict | None:
    """
    Given individual signals that fired in the same time window (each a
    dict with at least 'sharp_prob'/'true_prob' and
    'executable_odd'/'xbet_odd'/'odds',
    optionally 'correlation_group'), search all leg-count combinations
    from 1 up to `max_legs` legs and return the one with the highest
    net-of-tax EV at its numerically optimal stake — subject to
    is_combo_tax_viable(). Returns None if no combination (including
    single legs) is tax-viable.

    "Best" is ranked by EV in currency units at the optimal stake, not by
    raw combined odds — ranking by odds alone would bias toward long-shot
    combos with low win probability and high variance.

    By default (correlation_mode="forbid"), no returned combo will ever
    contain two legs sharing a correlation_group (see
    core/paim_engine.correlation_group) — see _combine_with_correlation
    for the "discount" alternative.

    Sizing-base unification (2026-07-11, operator decision — see
    core/risk_manager.py's module docstring for the mismatch this closes):
    the returned stake is capped at the SUM of the combo's own legs'
    dashboard kelly_pct (each signal's independently-persisted solo Kelly
    stake, in currency units at this same `bankroll` — the same figure
    templates/index.html shows per signal). The dashboard is now the
    canonical ceiling: a system can never recommend more than what its own
    component legs already imply on the dashboard, so core.risk_manager's
    kelly_pct-based exposure ledger is always a valid upper bound on real
    recommended capital, on both channels. Falls back to the pre-
    unification tax-engine-optimal stake alone when any leg is missing a
    persisted kelly_pct (legacy rows, or a caller that built signal dicts
    by hand rather than through run_engine._emit()) — there is no
    dashboard figure to reconcile against in that case.
    """
    if not signals:
        return None

    def _leg(sig):
        prob = sig.get("sharp_prob", sig.get("true_prob"))
        # `executable_odd` = signal EN MÉMOIRE produit par run_engine._emit
        # (depuis le 2026-08-27) ; `xbet_odd` = ligne relue de `signals`, dont
        # la colonne garde l'ancien nom ; `odds` = ligne du ledger. Les trois
        # portent le même prix, sous trois vocabulaires imposés par la base.
        odds = sig.get("executable_odd", sig.get("xbet_odd", sig.get("odds")))
        return {"true_prob": prob, "odds": odds, "correlation_group": sig.get("correlation_group"), "signal": sig}

    candidates = [_leg(s) for s in signals]
    candidates = [c for c in candidates if c["true_prob"] and c["odds"] and c["odds"] > 1.01]
    if not candidates:
        return None

    best = None
    n = min(max_legs, len(candidates))
    for k in range(1, n + 1):
        for combo in combinations(candidates, k):
            combo = list(combo)
            if not is_combo_tax_viable(combo, tax_rate, correlation_mode, rho):
                continue
            combined_p, combined_odds, ok = _combine_with_correlation(combo, correlation_mode, rho)
            if not ok:
                continue
            frac = optimal_stake_fraction(combined_p, combined_odds, tax_rate, kelly_multiplier)
            stake = frac * bankroll

            # Dashboard-basis cap — see this function's docstring.
            leg_signals = [leg["signal"] for leg in combo]
            if all(s.get("kelly_pct") is not None for s in leg_signals):
                dashboard_cap = sum((s.get("kelly_pct") or 0) / 100 * bankroll for s in leg_signals)
                stake = min(stake, dashboard_cap)

            if stake <= 0:
                continue
            ev = single_bet_ev_net_tax(stake, combined_odds, combined_p, tax_rate)
            if ev <= 0:
                continue
            if best is None or ev > best["ev"]:
                best = {
                    "legs": leg_signals,
                    "k": k,
                    "combined_prob": combined_p,
                    "combined_odds": combined_odds,
                    "stake": round(stake, 2),
                    "ev": round(ev, 2),
                }

    return best
