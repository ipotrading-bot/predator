"""
tests/test_audit_engine.py — ce qui reste à ce module après la suppression de
l'oracle web-search (2026-09-02, avec Groq/Tavily).

Les anciennes classes TestH2HFavorite / TestH2HUnresolvedSide /
TestNonH2HMarkets testaient `capture_closing_lines` version oracle (prix
« Pinnacle » demandé à un LLM, résolution du côté, refus des totals/spreads).
Ces contrats vivent désormais dans `core/closing_line.capture_from_exchange`
— testés par tests/test_closing_line_exchange.py (même côté, DNB exigé au
football, refus sinon) et tests/test_closing_line.py (câblage exchange du
job). Rien n'a été relâché : la garde a changé de fichier, pas de contrat.

Reste ici le contrat de signe du CLV consommé par la couche d'apprentissage.
"""
import pytest

from core.learning_layer import _clv_stats


class TestClvStatsConsumesSign:
    def test_positive_rate_counts_beating_the_close(self):
        # learning_layer._clv_stats consumes clv_pct_real with the same sign
        # convention the capture writes: > 0 = bettor beat the close.
        rows = [{"clv_pct_real": 5.0}, {"clv_pct_real": -3.0}, {"clv_pct_real": None}]
        stats = _clv_stats(rows)
        assert stats["n"] == 2                      # None excluded, never defaulted
        assert stats["positive_rate"] == pytest.approx(0.5)
        assert stats["avg_clv"] == pytest.approx(1.0)
