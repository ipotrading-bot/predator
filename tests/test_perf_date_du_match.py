"""Gardien — la colonne DATE de l'historique est le jour du MATCH (2026-09-30).

Elle affichait `created_at` du ledger, le jour du RÈGLEMENT : Espagne–Croatie,
joué le 29/09 à 18:45 UTC et réglé à 01:51, s'affichait au 30/09. Plainte
opérateur : « horaires faux ».
"""
from pathlib import Path

from core.perf_view import avec_date_du_match

_RACINE = Path(__file__).resolve().parent.parent


def _l(sid, created_at):
    return {"signal_id": sid, "created_at": created_at, "outcome": "LOSS"}


class TestDateDuMatch:
    def test_le_cas_espagne_croatie(self):
        h = avec_date_du_match([_l(10529, "2026-09-30 01:51:00+00")],
                               {"10529": "2026-09-29T18:45:00+00:00"})
        assert h[0]["match_time"] == "2026-09-29T18:45:00+00:00"

    def test_trie_par_coup_d_envoi_et_non_par_reglement(self):
        """Réglés dans l'ordre inverse de leurs matchs : l'historique suit
        les matchs."""
        h = avec_date_du_match(
            [_l(1, "2026-09-30 05:01:00+00"), _l(2, "2026-09-30 01:51:00+00")],
            {"1": "2026-09-29T18:00:00+00:00", "2": "2026-09-29T20:00:00+00:00"})
        assert [r["signal_id"] for r in h] == [2, 1]

    def test_sans_signal_retrouve_la_date_de_reglement_reste(self):
        h = avec_date_du_match([_l(9, "2026-08-10 12:00:00+00")], {})
        assert h[0]["match_time"] is None and h[0]["created_at"].startswith("2026-08-10")

    def test_la_ligne_d_origine_n_est_pas_modifiee(self):
        ligne = _l(1, "2026-09-30 05:01:00+00")
        avec_date_du_match([ligne], {"1": "2026-09-29T18:00:00+00:00"})
        assert "match_time" not in ligne


class TestBranchement:
    def test_la_route_lit_les_coups_d_envoi(self):
        src = (_RACINE / "api" / "index.py").read_text(encoding="utf-8")
        assert "history = _avec_date_du_match(history, _coups_d_envoi(sb, history))" in src

    def test_le_template_affiche_le_coup_d_envoi(self):
        tpl = (_RACINE / "templates" / "performance.html").read_text(encoding="utf-8")
        assert "r.match_time[8:10]" in tpl
