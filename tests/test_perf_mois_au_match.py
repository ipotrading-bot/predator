"""Gardien — /performance découpe ses mois au jour du MATCH (2026-10-02).

La colonne DATE de l'historique date le match depuis le 30/09, mais la
fenêtre, les cartes PAR MOIS et le mois choisi lisaient encore `created_at`,
le jour du RÈGLEMENT. Trois paris joués le 30/09 et réglés après minuit
comptaient dans la carte d'octobre (7–7 affiché pour 5–6 réel) et figuraient
dans son historique, datés « 30/09 ». Capture opérateur du 2026-10-02.

Second défaut de la même capture : « depuis août 2026 » était écrit en dur
alors que la fenêtre glissante avait quitté août le 1er octobre.
"""
import inspect
from datetime import datetime, timezone
from pathlib import Path

from core.perf_view import (ALL_MONTHS, avec_date_du_match, depuis_label, filter_rows,
                            mois_de, monthly_summary, rows_of_month)

_RACINE = Path(__file__).resolve().parent.parent
_OCTOBRE = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)


def _l(sid, regle, outcome="WIN", joue=None, sport="soccer"):
    ligne = {"signal_id": sid, "created_at": regle, "outcome": outcome,
             "odds": 2.0, "sport": sport}
    if joue:
        ligne["match_time"] = joue
    return ligne


# Le cas réel : joué le 30/09 à 23:30 UTC, réglé le 01/10 à 04:01.
_NYRB = _l(1, "2026-10-01 04:01:00+00", "WIN", "2026-09-30T23:30:00+00:00")
_OCT = _l(2, "2026-10-02 05:30:00+00", "LOSS", "2026-10-01T23:00:00+00:00")


class TestMoisDe:
    def test_le_mois_est_celui_du_match(self):
        assert mois_de(_NYRB) == "2026-09"
        assert mois_de(_OCT) == "2026-10"

    def test_sans_coup_d_envoi_le_reglement_sert_de_repli(self):
        assert mois_de(_l(3, "2026-10-01 12:31:00+00")) == "2026-10"
        assert mois_de({**_NYRB, "match_time": None}) == "2026-10"

    def test_une_ligne_sans_date_n_a_pas_de_mois(self):
        assert mois_de({"outcome": "WIN"}) == ""


class TestDecoupage:
    def test_le_mois_choisi_suit_le_match(self):
        rows = [_NYRB, _OCT]
        assert rows_of_month(rows, "2026-09") == [_NYRB]
        assert rows_of_month(rows, "2026-10") == [_OCT]
        assert len(rows_of_month(rows, ALL_MONTHS)) == 2

    def test_les_cartes_suivent_le_match(self):
        cartes = {c["month"]: c for c in monthly_summary([_NYRB, _OCT], 0.0)}
        assert (cartes["2026-09"]["wins"], cartes["2026-09"]["losses"]) == (1, 0)
        assert (cartes["2026-10"]["wins"], cartes["2026-10"]["losses"]) == (0, 1)

    def test_la_fenetre_suit_le_match(self):
        # Joué le 31/08, réglé le 01/09 : il appartient à août, sorti de la
        # fenêtre septembre–octobre — il ne gonfle plus la carte de septembre.
        aout = _l(4, "2026-09-01 02:00:00+00", "WIN", "2026-08-31T22:00:00+00:00")
        gardees = filter_rows([aout, _NYRB, _OCT], _OCTOBRE, months_shown=2)
        assert [r["signal_id"] for r in gardees] == [1, 2]

    def test_la_chaine_reelle_date_puis_decoupe(self):
        """Ce que fait la route : dater TOUTES les lignes, puis filtrer."""
        brutes = [_l(1, "2026-10-01 04:01:00+00"), _l(2, "2026-10-02 05:30:00+00", "LOSS")]
        datees = avec_date_du_match(brutes, {"1": "2026-09-30T23:30:00+00:00",
                                             "2": "2026-10-01T23:00:00+00:00"})
        rows = filter_rows(datees, _OCTOBRE, months_shown=2)
        assert [r["signal_id"] for r in rows_of_month(rows, "2026-10")] == [2]
        assert [r["signal_id"] for r in rows_of_month(rows, "2026-09")] == [1]


class TestDepuis:
    def test_le_libelle_est_le_plus_ancien_mois_affiche(self):
        assert depuis_label(["2026-10", "2026-09"]) == "depuis septembre 2026"
        assert depuis_label(["2026-08"]) == "depuis août 2026"

    def test_fenetre_vide(self):
        assert depuis_label([]) == ""

    def test_aucun_mois_en_dur_dans_la_page(self):
        tpl = (_RACINE / "templates" / "performance.html").read_text(encoding="utf-8")
        assert "depuis août" not in tpl and "depuis <b>août" not in tpl
        assert tpl.count("{{ depuis") >= 3


class _FauxSignals:
    """Double de `sb.table("signals")` : retient la taille de chaque lot."""

    def __init__(self, lots):
        self._lots, self._ids = lots, []

    def table(self, _nom):
        return self

    def select(self, _cols):
        return self

    def in_(self, _col, ids):
        self._ids = list(ids)
        self._lots.append(len(ids))
        return self

    def execute(self):
        return type("R", (), {"data": [{"id": int(i), "match_time": f"2026-10-01T00:00:00+00:00#{i}"}
                                       for i in self._ids]})()


class TestBranchement:
    def test_les_coups_d_envoi_se_lisent_par_lots(self):
        import api.index as dash
        lots: list = []
        rows = [{"signal_id": i} for i in range(1, 2 * dash._COUPS_D_ENVOI_LOT + 6)]
        out = dash._coups_d_envoi(_FauxSignals(lots), rows)
        assert len(out) == len(rows)
        assert lots == [dash._COUPS_D_ENVOI_LOT, dash._COUPS_D_ENVOI_LOT, 5]

    def test_une_panne_rend_ce_qui_a_ete_lu(self):
        import api.index as dash

        class _Casse(_FauxSignals):
            def execute(self):
                raise RuntimeError("réseau")

        assert dash._coups_d_envoi(_Casse([]), [{"signal_id": 1}]) == {}

    def test_la_route_n_a_plus_de_decoupage_au_reglement(self):
        import api.index as dash
        src = inspect.getsource(dash.performance)
        assert '(r.get("created_at") or "")[:7]' not in src
        assert "_mois_de(r)" in src and "depuis=_depuis_label(months)" in src

    def test_perf_view_n_a_qu_une_lecture_du_mois(self):
        from core import perf_view
        for f in (perf_view.filter_rows, perf_view.rows_of_month, perf_view.monthly_summary):
            src = inspect.getsource(f)
            assert "mois_de(r)" in src, f.__name__
            assert '(r.get("created_at") or "")[:7]' not in src, f.__name__
