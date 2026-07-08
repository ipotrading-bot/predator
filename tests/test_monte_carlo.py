"""
tests/test_monte_carlo.py — core/monte_carlo.py (§8 follow-up): bootstrap
bankroll simulation from real historical (outcome, kelly_pct, odds) data.
"""
import pytest

from core.monte_carlo import format_report, historical_returns, simulate


def _row(outcome, kelly_pct=10.0, odds=2.0):
    return {"outcome": outcome, "kelly_pct": kelly_pct, "odds": odds}


class TestHistoricalReturns:
    def test_win_return_is_positive_and_tax_adjusted(self):
        returns = historical_returns([_row("WIN", kelly_pct=10, odds=2.0)], tax_rate=0.20)
        # stake_frac=0.10, profit=0.10*(2.0-1)=0.10, net of 20% tax = 0.08
        assert returns == [pytest.approx(0.08)]

    def test_loss_return_is_negative_full_stake(self):
        returns = historical_returns([_row("LOSS", kelly_pct=10, odds=2.0)])
        assert returns == [pytest.approx(-0.10)]

    def test_push_and_unknown_excluded(self):
        rows = [_row("PUSH"), _row("UNKNOWN"), _row("closed"), _row("WIN")]
        assert len(historical_returns(rows)) == 1

    def test_missing_kelly_pct_or_odds_excluded(self):
        rows = [
            {"outcome": "WIN", "kelly_pct": None, "odds": 2.0},
            {"outcome": "WIN", "kelly_pct": 10.0, "odds": None},
            _row("WIN"),
        ]
        assert len(historical_returns(rows)) == 1

    def test_empty_input(self):
        assert historical_returns([]) == []


class TestSimulate:
    def _returns(self):
        # Mixed win/loss distribution, deterministic seed for reproducibility.
        return [0.08, 0.08, 0.08, -0.10, -0.10, 0.15, -0.10]

    def test_raises_on_empty_returns(self):
        with pytest.raises(ValueError):
            simulate([])

    def test_raises_on_invalid_trajectory_or_bet_count(self):
        with pytest.raises(ValueError):
            simulate(self._returns(), n_trajectories=0)
        with pytest.raises(ValueError):
            simulate(self._returns(), n_bets=0)

    def test_percentiles_are_monotonically_ordered(self):
        result = simulate(self._returns(), n_trajectories=500, n_bets=100, seed=42)
        eb = result["ending_bankroll"]
        assert eb["p05"] <= eb["p25"] <= eb["median"] <= eb["p75"] <= eb["p95"]

    def test_drawdown_percentiles_ordered_and_bounded(self):
        result = simulate(self._returns(), n_trajectories=500, n_bets=100, seed=42)
        dd = result["max_drawdown"]
        assert 0.0 <= dd["median"] <= dd["p75"] <= dd["p95"] <= 1.0

    def test_ruin_probability_in_valid_range(self):
        result = simulate(self._returns(), n_trajectories=500, n_bets=100, seed=42)
        assert 0.0 <= result["ruin_probability"] <= 1.0

    def test_all_positive_returns_never_ruins(self):
        result = simulate([0.05, 0.08, 0.10], n_trajectories=200, n_bets=50, seed=1)
        assert result["ruin_probability"] == 0.0
        assert result["ending_bankroll"]["p05"] > 1.0   # bankroll only ever grows

    def test_reproducible_with_same_seed(self):
        r1 = simulate(self._returns(), n_trajectories=100, n_bets=50, seed=7)
        r2 = simulate(self._returns(), n_trajectories=100, n_bets=50, seed=7)
        assert r1 == r2

    def test_different_seeds_can_differ(self):
        r1 = simulate(self._returns(), n_trajectories=100, n_bets=50, seed=1)
        r2 = simulate(self._returns(), n_trajectories=100, n_bets=50, seed=2)
        # Not a hard guarantee in general, but with this much data extremely
        # likely to differ — catches an accidentally-deterministic RNG.
        assert r1["ending_bankroll"]["median"] != r2["ending_bankroll"]["median"]

    def test_severe_losses_produce_high_ruin_probability(self):
        result = simulate([-0.30, -0.30, -0.30, 0.05], n_trajectories=300, n_bets=30, seed=3)
        assert result["ruin_probability"] > 0.5


class TestFormatReport:
    def test_report_contains_key_figures(self):
        result = simulate([0.08, -0.10, 0.15, -0.10], n_trajectories=100, n_bets=50, seed=5)
        report = format_report(result)
        assert "SIMULATION MONTE CARLO" in report
        assert "Bankroll final" in report
        assert "Drawdown maximal" in report
        assert "Probabilité de ruine" in report
        assert "corrélation réelle" in report   # independence caveat must be visible
