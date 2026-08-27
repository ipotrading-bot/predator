"""
tests/test_math_engine.py — Core edge/devig calculations.
Run: pytest tests/ -v
"""
import pytest

from core.paim_engine import compute_alpha
from core.math_engine import devig_prob, is_round_number_line


class TestComputeAlpha:
    # Depuis 2026-08-22, compute_alpha prend (cote_soft, prob_dévigorisée) et
    # rend l'EV vraie — plus jamais le ratio de prix contre une cote vigorisée
    # (voir la docstring de compute_alpha pour le post-mortem chiffré).
    #
    # Depuis 2026-08-27 le premier paramètre s'appelle `executable_odd` : la
    # cote soft doit être un prix qu'un book AFFICHE, pas un DNB dévigorisé.
    # Le nom `xbet_odd` a été retiré parce qu'il désignait, en football,
    # exactement le prix qu'il ne fallait pas passer.

    def test_positive_ev_within_range(self):
        edge, status = compute_alpha(executable_odd=2.10, sharp_prob=0.52, min_edge=1.5)
        assert status == "OK"
        assert edge == pytest.approx(9.2, abs=0.01)

    def test_near_coinflip_edge_not_gated_on_tax_here(self):
        # Garde de régression, incident 2026-07-08 : compute_alpha a
        # brièvement appliqué le plancher fiscal k=1 (le plus exigeant) et
        # jeté 22/22 candidats réels d'un scan. La viabilité fiscale reste
        # le travail exclusif de core.tax_engine.suggest_system()/
        # is_combo_tax_viable(), sur le combo réellement assemblé.
        edge, status = compute_alpha(executable_odd=2.09, sharp_prob=0.52, min_edge=1.5)
        assert status == "OK"
        assert edge == pytest.approx(8.68, abs=0.01)

    def test_ev_below_min_threshold_discarded(self):
        # +0.7% d'EV sous un plancher à 1.5%
        edge, status = compute_alpha(executable_odd=1.90, sharp_prob=0.53, min_edge=1.5)
        assert status == "DISCARD"

    def test_zero_or_invalid_inputs_discarded(self):
        assert compute_alpha(0, 0.5)[1] == "DISCARD"
        assert compute_alpha(1.90, 0)[1] == "DISCARD"
        assert compute_alpha(1.0, 0.5)[1] == "DISCARD"   # <= 1.01 guard
        assert compute_alpha(1.90, 1.0)[1] == "DISCARD"  # prob hors (0,1)

    def test_suspiciously_high_ev_discarded_above_max(self):
        # EV +50% = inversion de mapping ou donnée périmée, pas une opportunité
        edge, status = compute_alpha(executable_odd=5.0, sharp_prob=0.30, min_edge=1.5)
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
