"""
tests/test_mesure_outsider.py — mesurer le côté que le moteur n'évalue pas
(2026-09-27).

Hors football, `to_binary` ne teste que le FAVORI du book soft. Le week-end du
26/09, chaque combat MMA sortait à EV négatif sur ce côté. La question « l'outsider
porte-t-il un prix trop généreux ? » n'a aucun historique pour y répondre :
`_mesurer_outsider` en écrit une ligne de log par match, à zéro crédit.

Contrat gardé ici : c'est une MESURE. Elle n'ajoute, ne retire et ne change
aucun signal — élargir l'évaluation aux deux côtés est une décision opérateur.
"""
import logging
from datetime import datetime, timedelta, timezone

import run_engine

PIN = {"1": 1.50, "2": 2.70}           # favori A, outsider B


def _m(par_book):
    return {"id": "f1", "commence_time": (datetime.now(timezone.utc)
                                          + timedelta(hours=6)).isoformat(),
            "odds_1xbet": next(iter(par_book.values())),
            "h2h_par_book": par_book, "odds_pinnacle": PIN}


class TestMesure:
    def test_ev_de_loutsider_au_meilleur_book(self):
        lignes = []

        class L:
            def info(self, fmt, *a):
                lignes.append(fmt % a)
        ev = run_engine._mesurer_outsider(
            {"1xbet": {"1": 1.45, "2": 2.60}, "bet365": {"1": 1.44, "2": 2.90}},
            "mma", "A vs B", "🥋", "B", "2", 0.40, L())
        assert round(ev, 4) == round(0.40 * 2.90 - 1, 4)
        assert "bet365" in lignes[0] and "B @ 2.90" in lignes[0]
        assert "jamais émis" in lignes[0]

    def test_sans_prix_rien(self):
        assert run_engine._mesurer_outsider({"1xbet": {"1": 1.5}}, "mma", "A vs B",
                                            "🥋", "B", "2", 0.4, logging.getLogger("t")) is None
        assert run_engine._mesurer_outsider({"1xbet": {"2": 2.5}}, "mma", "A vs B",
                                            "🥋", "B", "2", 1.0, logging.getLogger("t")) is None


class TestJamaisUneEmission:
    def test_le_moteur_mesure_loutsider_sans_changer_les_signaux(self, caplog):
        """Outsider très généreux chez Bet365 (3,40 pour ~37 % sharp, EV ≈ +27 %) :
        il est MESURÉ dans le log, et le favori reste le seul côté évalué."""
        par_book = {"1xbet": {"1": 1.45, "2": 2.60}, "bet365": {"1": 1.44, "2": 3.40}}
        out: list = []
        with caplog.at_level(logging.INFO):
            run_engine._process_h2h(_m(par_book), "A vs B", "mma", "UFC", "A", "B", "🥋",
                                    out, None, datetime.now(timezone.utc),
                                    logging.getLogger("t"), min_edge=2.0)
        assert any("OUTSIDER" in r.getMessage() and "B @ 3.40" in r.getMessage()
                   for r in caplog.records)
        assert all(s.get("selection_name") != "B" for s in out)

    def test_le_football_nest_pas_mesure(self, caplog):
        """Le DNB du football engage deux jambes chez un book : hors sujet ici."""
        par_book = {"1xbet": {"1": 1.95, "X": 3.50, "2": 4.00}}
        m = {**_m(par_book), "odds_pinnacle": {"1": 1.90, "X": 3.60, "2": 4.20}}
        with caplog.at_level(logging.INFO):
            run_engine._process_h2h(m, "A vs B", "soccer", "L1", "A", "B", "⚽", [], None,
                                    datetime.now(timezone.utc), logging.getLogger("t"),
                                    min_edge=1.0)
        assert not any("OUTSIDER" in r.getMessage() for r in caplog.records)
