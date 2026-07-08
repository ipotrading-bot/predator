"""
tests/test_stats_utils.py — core/stats_utils.py (Task 4): Wilson CI,
tax-adjusted breakeven probability, Brier score, calibration buckets.
"""
import pytest

from core.stats_utils import (
    brier_score,
    bucket_predictions,
    p_breakeven,
    wilson_ci,
)


class TestWilsonCI:
    def test_matches_known_reference_interval_50_of_100(self):
        # Standard reference value: Wilson 95% CI for 50/100 ~= [0.404, 0.596]
        lower, upper = wilson_ci(wins=50, n=100)
        assert lower == pytest.approx(0.404, abs=0.001)
        assert upper == pytest.approx(0.596, abs=0.001)

    def test_wider_interval_at_smaller_sample_size(self):
        # Same observed rate (60%), fewer samples -> wider interval.
        lo_small, hi_small = wilson_ci(wins=6, n=10)
        lo_big, hi_big = wilson_ci(wins=60, n=100)
        assert (hi_small - lo_small) > (hi_big - lo_big)

    def test_zero_samples_returns_full_range(self):
        assert wilson_ci(0, 0) == (0.0, 1.0)

    def test_bounds_stay_within_unit_interval(self):
        lower, upper = wilson_ci(wins=10, n=10)   # 100% observed
        assert 0.0 <= lower <= upper <= 1.0


class TestPBreakeven:
    def test_matches_the_simplified_1_25_over_odds_formula_at_20pct_tax(self):
        assert p_breakeven(2.5, tax_rate=0.20) == pytest.approx(1.25 / 2.5)
        assert p_breakeven(5.0, tax_rate=0.20) == pytest.approx(1.25 / 5.0)

    def test_higher_odds_need_lower_win_probability_to_break_even(self):
        assert p_breakeven(2.0) > p_breakeven(4.0)

    def test_invalid_odds_returns_certainty(self):
        assert p_breakeven(1.0) == 1.0
        assert p_breakeven(0.5) == 1.0


class TestBrierScore:
    def test_perfect_predictions_score_zero(self):
        assert brier_score([(1.0, 1), (0.0, 0)]) == 0.0

    def test_perfectly_wrong_predictions_score_one(self):
        assert brier_score([(1.0, 0), (0.0, 1)]) == 1.0

    def test_uninformative_coin_flip_scores_quarter(self):
        assert brier_score([(0.5, 1), (0.5, 0)]) == pytest.approx(0.25)

    def test_empty_predictions_returns_none(self):
        assert brier_score([]) is None


class TestBucketPredictions:
    def test_splits_into_correct_buckets(self):
        preds = [(0.55, 1), (0.65, 0), (0.72, 1), (0.85, 1)]
        buckets = bucket_predictions(preds)
        assert buckets["50-60%"]["n"] == 1
        assert buckets["60-70%"]["n"] == 1
        assert buckets["70-80%"]["n"] == 1
        assert buckets["80-100%"]["n"] == 1

    def test_empty_bucket_reports_none_not_crash(self):
        preds = [(0.55, 1)]
        buckets = bucket_predictions(preds)
        assert buckets["80-100%"]["n"] == 0
        assert buckets["80-100%"]["win_rate"] is None

    def test_detects_overconfidence_within_a_bucket(self):
        # All "80%+ confident" picks that only win half the time — a
        # miscalibration a plain aggregate win rate could hide if mixed
        # with genuinely strong lower-confidence picks elsewhere.
        preds = [(0.85, 1), (0.85, 0), (0.85, 1), (0.85, 0)]
        buckets = bucket_predictions(preds)
        b = buckets["80-100%"]
        assert b["win_rate"] == 0.5
        assert b["avg_predicted"] == pytest.approx(0.85)
        assert b["win_rate"] < b["avg_predicted"]
