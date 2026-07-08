"""
core/monte_carlo.py — PAIM v9.5 — Bankroll growth simulator.

Bootstraps trajectories from the REAL observed (outcome, kelly_pct, odds)
distribution in ai_learning_ledger — not a theoretical edge assumption —
to produce a DISTRIBUTION of outcomes (percentiles, drawdown, ruin
probability) rather than a single optimistic number extrapolated from a
few weeks of results. A net edge this size (2-4% post-tax, 10-15%
fractional Kelly — see core/constants.py's KELLY_FRACTION) has modest
expected growth and normal-not-alarming drawdowns of 20-40% along the
way; this module exists to make that variance visible before a
withdrawal decision, not to promise a number.

Independence caveat: bootstrapping resamples bets i.i.d., but real
signals aren't fully independent (see core/tax_engine.py's
correlation_group handling) — same-day/same-league/correlated-market
legs cluster in reality. This simulation therefore likely UNDERSTATES
the true variance of drawdowns; treat its ranges as optimistic, not
pessimistic, bounds.
"""
import random

from core.constants import TAX_RATE as DEFAULT_TAX_RATE

DEFAULT_N_TRAJECTORIES = 1000
DEFAULT_N_BETS = 200          # ~ a few weeks-to-months of combo-only signal volume
RUIN_FRACTION = 0.10          # bankroll <= 10% of starting counts as "ruin"


def historical_returns(ledger_rows: list[dict], tax_rate: float = DEFAULT_TAX_RATE) -> list[float]:
    """
    Per-bet fractional bankroll return for every decisive (WIN/LOSS) row
    with a recorded kelly_pct and odds — the empirical distribution this
    simulator bootstraps from. kelly_pct is a % of the same fixed
    reference bankroll every signal used (see core/learning_layer.py's
    ROI calc for the same convention), so these fractions compose
    directly without needing the absolute € amount. Tax is applied here
    (net profit only, matching core/tax_engine.py's model) — this is
    "money actually in pocket" return, not the gross price movement.
    """
    returns = []
    for r in ledger_rows:
        if r.get("outcome") not in ("WIN", "LOSS"):
            continue
        kelly_pct = r.get("kelly_pct")
        odds = r.get("odds")
        if not kelly_pct or not odds:
            continue
        stake_frac = kelly_pct / 100
        if r["outcome"] == "WIN":
            returns.append(stake_frac * (odds - 1) * (1 - tax_rate))
        else:
            returns.append(-stake_frac)
    return returns


def _percentile(sorted_vals: list[float], p: float) -> float:
    idx = min(len(sorted_vals) - 1, max(0, round(p * (len(sorted_vals) - 1))))
    return sorted_vals[idx]


def simulate(returns: list[float],
            n_trajectories: int = DEFAULT_N_TRAJECTORIES,
            n_bets: int = DEFAULT_N_BETS,
            starting_bankroll: float = 1.0,
            ruin_fraction: float = RUIN_FRACTION,
            seed: int | None = None) -> dict:
    """
    Bootstrap `n_trajectories` independent paths of `n_bets` bets each,
    resampling WITH replacement from the empirical `returns` distribution
    and compounding (bankroll *= 1+r each step — this is what makes it a
    Kelly-style GROWTH simulation rather than a flat-stake one). Returns
    ending-bankroll percentiles, max-drawdown percentiles, and the
    fraction of trajectories that ever touched `ruin_fraction` of the
    starting bankroll.

    Raises ValueError on an empty `returns` list — there's nothing
    honest to bootstrap from, and silently returning zeros/None would be
    indistinguishable from "everything is fine."
    """
    if not returns:
        raise ValueError("no historical returns to bootstrap from — need real settled signals first")
    if n_trajectories < 1 or n_bets < 1:
        raise ValueError("n_trajectories and n_bets must be >= 1")

    rng = random.Random(seed)
    ruin_threshold = starting_bankroll * ruin_fraction

    ending_values = []
    max_drawdowns = []
    ruin_count = 0

    for _ in range(n_trajectories):
        bankroll = starting_bankroll
        peak = bankroll
        max_dd = 0.0
        ruined = False
        for _ in range(n_bets):
            bankroll = max(0.0, bankroll * (1 + rng.choice(returns)))
            peak = max(peak, bankroll)
            if peak > 0:
                max_dd = max(max_dd, (peak - bankroll) / peak)
            if bankroll <= ruin_threshold:
                ruined = True
        ending_values.append(bankroll)
        max_drawdowns.append(max_dd)
        if ruined:
            ruin_count += 1

    ending_values.sort()
    max_drawdowns.sort()

    return {
        "n_trajectories": n_trajectories,
        "n_bets": n_bets,
        "n_historical_returns": len(returns),
        "ending_bankroll": {
            "p05":    _percentile(ending_values, 0.05),
            "p25":    _percentile(ending_values, 0.25),
            "median": _percentile(ending_values, 0.50),
            "p75":    _percentile(ending_values, 0.75),
            "p95":    _percentile(ending_values, 0.95),
        },
        "max_drawdown": {
            "median": _percentile(max_drawdowns, 0.50),
            "p75":    _percentile(max_drawdowns, 0.75),
            "p95":    _percentile(max_drawdowns, 0.95),
        },
        "ruin_probability": ruin_count / n_trajectories,
    }


def format_report(result: dict) -> str:
    """Telegram/console-friendly rendering of simulate()'s output."""
    eb = result["ending_bankroll"]
    dd = result["max_drawdown"]
    return (
        f"🎲 *SIMULATION MONTE CARLO* — {result['n_trajectories']} trajectoires × {result['n_bets']} paris\n"
        f"Basé sur {result['n_historical_returns']} résultats réels (ai_learning_ledger)\n\n"
        f"*Bankroll final* (départ 100%):\n"
        f"  P05: {eb['p05']*100:.0f}%  ·  P25: {eb['p25']*100:.0f}%  ·  Médiane: {eb['median']*100:.0f}%  ·  "
        f"P75: {eb['p75']*100:.0f}%  ·  P95: {eb['p95']*100:.0f}%\n\n"
        f"*Drawdown maximal*:\n"
        f"  Médiane: {dd['median']*100:.0f}%  ·  P75: {dd['p75']*100:.0f}%  ·  P95: {dd['p95']*100:.0f}%\n\n"
        f"*Probabilité de ruine* (≤{int(RUIN_FRACTION*100)}% bankroll): {result['ruin_probability']*100:.1f}%\n\n"
        f"⚠️ Hypothèse d'indépendance entre paris — la corrélation réelle entre jambes "
        f"sous-estime probablement la vraie variance des drawdowns ci-dessus."
    )
