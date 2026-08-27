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
    """Le modèle de taxe du dépôt frappe le GAIN NET d'un pari gagnant, jamais
    le payout brut. Le point mort est donc `1 / (1 + (cote−1)(1−taux))`.

    Le test précédent enchâssait `1.25/cote`, c'est-à-dire `1/((1−taux)·cote)`
    — la taxe sur le PAYOUT BRUT, modèle qu'aucune autre fonction du dépôt
    n'applique. Il ne pouvait donc pas détecter l'erreur : il la définissait.
    """

    def test_le_point_mort_equilibre_le_gain_net_et_la_mise_perdue(self):
        # Définition : à p = point mort, p·(cote−1)·(1−taux) == (1−p).
        for odds in (1.30, 1.85, 2.50, 5.00):
            for tax in (0.0, 0.20, 0.35):
                p = p_breakeven(odds, tax_rate=tax)
                assert p * (odds - 1) * (1 - tax) == pytest.approx(1 - p, abs=1e-3), \
                    (odds, tax)

    def test_sans_taxe_le_point_mort_est_la_probabilite_implicite(self):
        for odds in (1.30, 2.00, 4.00):
            assert p_breakeven(odds, tax_rate=0.0) == pytest.approx(1 / odds, abs=1e-4)

    def test_la_taxe_releve_le_point_mort(self):
        for odds in (1.30, 2.00, 4.00):
            assert p_breakeven(odds, tax_rate=0.20) > p_breakeven(odds, tax_rate=0.0)

    def test_lancienne_formule_surestimait_le_taux_requis(self):
        # Garde de non-régression chiffrée : à cote courte l'écart dépassait
        # 15 points, assez pour déclarer perdante une bande rentable.
        ancienne = 1 / ((1 - 0.20) * 1.35)          # 0.9259
        assert p_breakeven(1.35, tax_rate=0.20) == pytest.approx(0.7812, abs=1e-4)
        assert ancienne - p_breakeven(1.35, tax_rate=0.20) > 0.14

    def test_une_bande_a_824_pct_sur_cote_135_est_rentable(self):
        # Le cas réel du 2026-08-02 que l'ancienne formule condamnait.
        p, odds = 0.824, 1.35
        assert p > p_breakeven(odds, tax_rate=0.20)
        ev_nette = p * (odds - 1) * 0.8 - (1 - p)
        assert ev_nette > 0

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
