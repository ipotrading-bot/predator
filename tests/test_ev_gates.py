"""
tests/test_ev_gates.py — les trois gates posés le 2026-08-22, après l'audit
du ledger réglé (Brier de sharp_prob pire que la proba implicite du book
soft, pente de recalibration 0,12, CLV réel nul) :

1. l'edge émis est l'EV VRAIE (prob dévigorisée × cote soft − 1), plus
   jamais le ratio de prix contre une cote sharp encore vigorisée ;
2. gate worst-case : l'EV doit rester positive sous la méthode de
   dévigorisation la plus défavorable (pratique standard des outils pros) ;
3. mise Kelly nulle = signal refusé — 37 signaux sur 91 étaient partis avec
   kelly_pct=0, c'est-à-dire refusés par la couche de mise et publiés quand
   même ;
plus le plancher EV_EDGE_FLOOR, appliqué DANS _emit pour que la règle AH0
(min_edge=0.8) ne puisse pas le contourner.
"""
import logging
from datetime import datetime, timedelta, timezone

import pytest

import run_engine
from core.constants import EV_EDGE_FLOOR
from core.math_engine import devig, devig_bounds

log = logging.getLogger("test")


def _now():
    return datetime.now(timezone.utc)


def _kickoff(hours=3):
    return (_now() + timedelta(hours=hours)).isoformat()


# ── Régime de référence des tests de gates (A2, taxe rétablie à 20 %) ─────
# Chaque gate doit être éprouvé dans un régime où LUI SEUL peut refuser le
# signal. Depuis que TAX_RATE vaut 0.20, la mise Kelly refuse d'elle-même
# toute EV brute inférieure au point mort après taxe — et ce point mort monte
# vite avec la cote : +4,84 % à cote 1,30, mais +10,47 % à cote 1,90, soit
# AU-DESSUS de SUSPECT_EDGE. Éprouver un gate à cote 1,90 le rendrait donc
# indistinguable du gate de mise : le test passerait même si le gate étudié
# était supprimé.
# Régime retenu : cote 1.30, p 0.83 → +7,90 % brut, +2,92 % net, f* = 0,1217.
COTE_VIABLE = 1.30
PROB_VIABLE = 0.83        # EV nette positive, mise Kelly non nulle
PROB_VIABLE_CONS = 0.82   # borne worst-case encore positive


def _emit(signals, executable_odd, sharp_prob, sharp_prob_cons=None, min_edge=1.5,
          sport="soccer"):
    run_engine._emit(signals, None, _now(), log, "Arsenal vs Chelsea", sport,
                     "PL", "h2h", "AH 0.0", executable_odd, 1.25, sharp_prob, "⚽",
                     selection_name="Arsenal", min_edge=min_edge,
                     match_time=_kickoff(), match_id="m1",
                     sharp_prob_cons=sharp_prob_cons)


class TestDevigEnsemble:
    def test_probabilities_sum_to_one(self):
        for odds in ([1.90, 1.98], [1.08, 9.0], [2.60, 3.30, 2.70]):
            assert sum(devig(odds)) == pytest.approx(1.0, abs=1e-9)

    def test_heavy_favourite_above_multiplicative(self):
        # L'ancien devig_prob rendait 0.8720 ici — sous le multiplicatif
        # (0.8791), le pire estimateur de la littérature. L'ensemble doit
        # rester au-dessus.
        p_med, p_cons = devig_bounds(1.10, 8.0)
        assert p_med > 0.8791
        assert p_cons == pytest.approx(0.8791, abs=1e-3)   # borne = multiplicatif ici

    def test_conservative_never_exceeds_median(self):
        for own, other in ((1.10, 8.0), (1.44, 2.85), (1.90, 1.98), (2.10, 1.80)):
            p_med, p_cons = devig_bounds(own, other)
            assert p_cons <= p_med + 1e-9

    def test_fair_book_is_identity(self):
        # Paire déjà sans marge : les trois méthodes coïncident.
        p_med, p_cons = devig_bounds(2.0, 2.0)
        assert p_med == p_cons == 0.5

    def test_invalid_input_degrades_to_zero(self):
        assert devig_bounds(1.0, 8.0) == (0.0, 0.0)
        assert devig([1.90, 0]) == [0.0, 0.0]


class TestWorstCaseGate:
    def test_median_positive_but_worst_case_negative_is_discarded(self):
        signals = []
        # La MÉDIANE est pleinement viable (+7,90 % brut, +2,92 % net, mise
        # Kelly non nulle) : seul le gate worst-case peut refuser ce signal.
        # Le supprimer ferait passer le test au vert — c'est ce qui le rend
        # probant.
        _emit(signals, executable_odd=COTE_VIABLE, sharp_prob=PROB_VIABLE,
              sharp_prob_cons=0.76)   # 0.76 × 1.30 − 1 = −1,2 %
        assert signals == []

    def test_robust_ev_passes(self):
        signals = []
        _emit(signals, executable_odd=COTE_VIABLE, sharp_prob=PROB_VIABLE,
              sharp_prob_cons=PROB_VIABLE_CONS)
        assert len(signals) == 1
        assert signals[0]["edge_pct"] == pytest.approx(7.9, abs=0.01)

    def test_gate_optional_when_no_bound_available(self):
        # Branche oracle : pas de côté opposé, pas de borne — le gate ne
        # s'applique pas, les autres gardes suffisent.
        signals = []
        _emit(signals, executable_odd=COTE_VIABLE, sharp_prob=PROB_VIABLE,
              sharp_prob_cons=None)
        assert len(signals) == 1


class TestKellyGate:
    def test_zero_stake_never_emits(self, monkeypatch):
        # Le MÊME signal, viable à 20 % de taxe, cesse de l'être à 50 % :
        # optimal_stake_fraction rend 0 → il ne doit pas sortir. Le contraste
        # entre les deux taux est ce qui prouve que la mise est fiscalisée.
        monkeypatch.setattr(run_engine, "_TAX_RATE", 0.50)
        signals = []
        _emit(signals, executable_odd=COTE_VIABLE, sharp_prob=PROB_VIABLE,
              sharp_prob_cons=PROB_VIABLE_CONS)
        assert signals == []

    def test_kelly_pct_comes_from_tax_engine(self):
        from core.tax_engine import optimal_stake_fraction
        from core.constants import KELLY_FRACTION, TAX_RATE
        signals = []
        _emit(signals, executable_odd=COTE_VIABLE, sharp_prob=PROB_VIABLE,
              sharp_prob_cons=PROB_VIABLE_CONS)
        expected = round(optimal_stake_fraction(
            PROB_VIABLE, COTE_VIABLE, tax_rate=TAX_RATE,
            kelly_multiplier=KELLY_FRACTION["soccer"]) * 100, 2)
        assert signals[0]["kelly_pct"] == pytest.approx(expected, abs=0.01)
        assert signals[0]["kelly_pct"] > 0


class TestEvFloor:
    def test_floor_cannot_be_undercut_by_caller(self):
        # min_edge=0.8 (règle AH0) + EV à +1.0 % : sous le plancher → refus.
        # 0.7769 × 1.30 − 1 = +1,0 %.
        assert EV_EDGE_FLOOR > 1.0
        signals = []
        _emit(signals, executable_odd=COTE_VIABLE, sharp_prob=0.7769,
              sharp_prob_cons=0.7769, min_edge=0.8)
        assert signals == []

    def test_learned_threshold_above_floor_is_kept(self):
        # Un seuil appris PLUS EXIGEANT que le plancher reste maître.
        floor = run_engine._segment_min_edge({"soccer": 4.0}, {}, "soccer", "h2h")
        assert floor == 4.0

    def test_learned_threshold_below_floor_is_clamped(self):
        floor = run_engine._segment_min_edge({"soccer": 0.5}, {}, "soccer", "h2h")
        assert floor == EV_EDGE_FLOOR
