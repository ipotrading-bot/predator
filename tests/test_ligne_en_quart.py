"""
tests/test_ligne_en_quart.py — la ligne asiatique en QUART (2026-09-15).

Une ligne en quart (±0.25, ±0.75, 2.25…) se règle chez le book en
DEMI-gain ou DEMI-perte : la mise se partage entre les deux lignes voisines.
Ni le ledger ni ses lecteurs ne savent représenter une demi-issue — « WIN »
et « LOSS » sont écrits en dur à une dizaine d'endroits. Décision opérateur
du 2026-09-15 (option A) : on ne les ÉMET pas, et on ne les RÈGLE pas.
Aucune n'avait été émise depuis le 2026-08-02 (égalité exacte de ligne avec
l'exchange) : la garde ne coûte donc aucun volume mesurable.

Ce que ces tests interdisent : qu'un quart ressorte en WIN/LOSS PLEIN, qui
compterait une demi-perte comme une perte entière dans toutes les stats.
"""
import logging

import run_engine
from core.paim_engine import ligne_en_quart
from core.settlement import determine_outcome

log = logging.getLogger("PREDATOR.test")


class TestLaReconnaissanceDunQuart:
    def test_les_quarts_sont_reconnus_dans_les_deux_signes(self):
        for point in (0.25, 0.75, 1.25, 1.75, 2.25, 3.75,
                      -0.25, -0.75, -1.25, -2.25):
            assert ligne_en_quart(point) is True, point

    def test_les_demies_et_les_entieres_ne_sont_pas_des_quarts(self):
        """Une ligne en demi (±0.5) ne connaît pas le PUSH mais reste un
        WIN/LOSS plein ; une entière peut pousser. Les deux sont réglables."""
        for point in (0.0, -0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0,
                      -0.5, -1.0, -1.5, -2.5):
            assert ligne_en_quart(point) is False, point


class TestLeReglementRefuseDeDeviner:
    """Le règlement rend UNKNOWN : la ligne reste ouverte et visible, elle
    n'entre pas dans les stats sous une issue fausse."""

    def test_un_total_en_quart_rend_unknown(self):
        assert determine_outcome("soccer", "totals_over", "Over 2.25",
                                 "A FC", "B FC", 2, 1) == "UNKNOWN"
        assert determine_outcome("soccer", "totals_under", "Under 2.75",
                                 "A FC", "B FC", 1, 0) == "UNKNOWN"

    def test_un_handicap_en_quart_rend_unknown(self):
        assert determine_outcome("soccer", "spreads_home", "A FC -0.75",
                                 "A FC", "B FC", 2, 0) == "UNKNOWN"
        assert determine_outcome("soccer", "spreads_away", "B FC +1.25",
                                 "A FC", "B FC", 0, 1) == "UNKNOWN"

    def test_les_lignes_reglables_le_restent(self):
        """Non-régression : la garde ne doit pas emporter les demies et les
        entières, qui sont l'immense majorité du ledger."""
        assert determine_outcome("soccer", "totals_over", "Over 2.5",
                                 "A FC", "B FC", 2, 1) == "WIN"
        assert determine_outcome("soccer", "totals_under", "Under 2.5",
                                 "A FC", "B FC", 1, 0) == "WIN"
        assert determine_outcome("soccer", "totals_over", "Over 3.0",
                                 "A FC", "B FC", 2, 1) == "PUSH"
        assert determine_outcome("soccer", "spreads_home", "A FC -1.5",
                                 "A FC", "B FC", 2, 0) == "WIN"
        assert determine_outcome("soccer", "spreads_home", "A FC -1.0",
                                 "A FC", "B FC", 1, 0) == "PUSH"


class TestLEmissionRefuseAvantDeParier:
    """Refuser au RÈGLEMENT ne suffit pas : un pari émis puis jamais réglable
    est un trou dans l'échantillon (et un pari réellement joué par
    l'opérateur). La garde est aussi à l'émission."""

    @staticmethod
    def _ligne(soft_pt, sharp_pt):
        return run_engine._meme_ligne({"point": soft_pt}, {"point": sharp_pt},
                                      "spreads", "A vs B", "⚽", log)

    def test_deux_books_daccord_sur_un_quart_nemettent_rien(self, caplog):
        with caplog.at_level(logging.INFO, logger="PREDATOR"):
            assert self._ligne(-0.75, -0.75) is None
            assert self._ligne(2.25, 2.25) is None
        assert "QUARTSKIP" in caplog.text, "un refus muet est indébogable"

    def test_les_demies_et_entieres_passent_toujours(self):
        assert self._ligne(-1.5, -1.5) == -1.5
        assert self._ligne(0.0, 0.0) == 0.0
        assert self._ligne(2.0, 2.0) == 2.0
