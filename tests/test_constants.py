"""
tests/test_constants.py — core/constants.py risk_flag().
Regression guard: run_engine.py used to reimplement this exact ternary
inline with its own per-sport `elite` threshold instead of calling the
"single source of truth" function (which only ever imported it as `_risk`,
dead code, never called) — the two could silently drift. risk_flag() now
takes the per-sport threshold as a parameter so there's exactly one
implementation for both the generic and sport-aware cases.
"""
from core.constants import BASKETBALL_ELITE_EDGE, ELITE_EDGE, SOCCER_ELITE_EDGE, risk_flag


class TestRiskFlag:
    def test_default_elite_boundaries(self):
        assert risk_flag(ELITE_EDGE * 2) == "HIGH_VALUE"
        assert risk_flag(ELITE_EDGE) == "VALUE"
        assert risk_flag(ELITE_EDGE - 0.01) == "LOW_VALUE"

    def test_sport_specific_soccer_threshold(self):
        # Soccer's tighter AH0 threshold (1.5%) means edges that would be
        # LOW_VALUE under the generic 2.5% boundary are VALUE for soccer.
        edge = SOCCER_ELITE_EDGE
        assert risk_flag(edge, elite=SOCCER_ELITE_EDGE) == "VALUE"
        assert risk_flag(edge, elite=ELITE_EDGE) == "LOW_VALUE"

    def test_sport_specific_basketball_threshold(self):
        edge = BASKETBALL_ELITE_EDGE * 2
        assert risk_flag(edge, elite=BASKETBALL_ELITE_EDGE) == "HIGH_VALUE"

    def test_zero_and_negative_edge_is_low_value(self):
        assert risk_flag(0) == "LOW_VALUE"
        assert risk_flag(-1) == "LOW_VALUE"
