"""
tests/test_math_engine.py — Core edge/devig calculations.
Run: pytest tests/ -v
"""
import pytest

from core.paim_engine import compute_alpha
from core.math_engine import devig_prob, is_round_number_line


class TestComputeAlpha:
    def test_positive_edge_within_range(self):
        # Strong favorite (true_prob=0.80 from pinnacle_price=1.25) — tax
        # floor at this probability is only ~5.75% (see
        # core.constants.min_edge_for_k), comfortably cleared by a 10% edge.
        edge, status = compute_alpha(xbet_odd=1.375, pinnacle_price=1.25, min_edge=1.5)
        assert status == "OK"
        assert edge == pytest.approx(10.0, abs=0.01)

    def test_near_coinflip_edge_rejected_by_tax_floor_even_if_above_flat_min_edge(self):
        # Regression guard for PAIM v9.5: at true_prob~0.52 (near even-money),
        # TAX_RATE=0.20 withheld on winnings-only requires ~13.9% raw edge to
        # break even (core.tax_engine.min_edge_required) — far above the old
        # flat min_edge=1.5%. An 8.29% edge here would have passed the old
        # flat-threshold-only check but is a guaranteed net-of-tax loser.
        edge, status = compute_alpha(xbet_odd=2.09, pinnacle_price=1.93, min_edge=1.5)
        assert status == "DISCARD"
        assert edge == pytest.approx(8.29, abs=0.01)

    def test_edge_below_min_threshold_discarded(self):
        edge, status = compute_alpha(xbet_odd=1.90, pinnacle_price=1.89, min_edge=1.5)
        assert status == "DISCARD"

    def test_zero_or_invalid_odds_discarded(self):
        assert compute_alpha(0, 1.90)[1] == "DISCARD"
        assert compute_alpha(1.90, 0)[1] == "DISCARD"
        assert compute_alpha(1.0, 1.90)[1] == "DISCARD"  # <= 1.01 guard

    def test_suspiciously_high_edge_discarded_above_max(self):
        # Guards against stale/bad data producing an unrealistic edge
        edge, status = compute_alpha(xbet_odd=5.0, pinnacle_price=1.5, min_edge=1.5)
        assert status == "DISCARD"


class TestDevigProb:
    def test_devig_prob_removes_vig_symmetric(self):
        # Two-way market, equal odds → true prob should be ~50% each side
        p = devig_prob(own_odd=1.90, other_odd=1.90)
        assert p == pytest.approx(0.5, abs=0.01)

    def test_devig_prob_favors_lower_odd_side(self):
        # Lower odd = higher implied probability even after devig
        p_fav = devig_prob(own_odd=1.50, other_odd=2.80)
        p_dog = devig_prob(own_odd=2.80, other_odd=1.50)
        assert p_fav > p_dog
        assert p_fav + p_dog == pytest.approx(1.0, abs=0.01)


class TestRoundLineDetection:
    """Exercises the actual is_round_number_line() used by
    run_engine.py::_process_totals to gate the MLB push-probability
    adjustment (PUSH_PROB_ROUND_LINE) — regression guard for the push-risk
    fix, against the real function rather than a copy of its logic."""

    def test_whole_number_totals_flagged_as_round(self):
        assert is_round_number_line(8) is True
        assert is_round_number_line(9) is True
        assert is_round_number_line(10.0) is True

    def test_half_point_totals_not_flagged(self):
        assert is_round_number_line(8.5) is False
        assert is_round_number_line(9.5) is False

    def test_none_or_zero_point_not_flagged(self):
        assert is_round_number_line(None) is False
        assert is_round_number_line(0) is False
