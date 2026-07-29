"""
tests/test_dashboard_groups.py — api/index.py::_group_by_match()

Un match produit jusqu'à 3 signaux (h2h + totals + spreads). Affichés à plat,
ils se lisaient comme des doublons : la carte n'affichait que le nom du match,
le libellé du marché n'existant que dans la modale. Le dashboard les regroupe
maintenant en une carte par match.

L'invariant critique est l'index : le template rend `openModal(leg.idx)` et le
JS indexe dans `SIGNALS = {{ signals|tojson }}`, la liste PLATE. Un index de
jambe qui dérive ouvrirait la modale d'un autre pari que celui cliqué.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.index import _group_by_match


def _sig(match, market_key, edge, flag="VALUE", sport="soccer",
         match_time="2026-07-22T19:00:00+00:00", match_id="m1"):
    return {
        "match": match, "market_key": market_key, "market": market_key.upper(),
        "edge_pct": edge, "risk_flag": flag, "sport": sport,
        "match_time": match_time, "match_id": match_id, "league": "PL",
    }


class TestGrouping:
    def test_markets_of_one_match_collapse_into_one_card(self):
        signals = [
            _sig("Arsenal vs Chelsea", "h2h", 3.2),
            _sig("Arsenal vs Chelsea", "totals_over", 2.7),
            _sig("Arsenal vs Chelsea", "spreads_home", 1.9),
        ]
        groups = _group_by_match(signals)
        assert len(groups) == 1
        assert len(groups[0]["legs"]) == 3

    def test_distinct_matches_stay_separate(self):
        signals = [
            _sig("Arsenal vs Chelsea", "h2h", 3.2, match_id="a"),
            _sig("Bayern vs Dortmund", "h2h", 2.1, match_id="b"),
        ]
        assert len(_group_by_match(signals)) == 2

    def test_same_match_from_two_sources_still_one_card(self):
        # The Odds API (uuid) et la recherche web (id dérivé des noms) donnent
        # deux match_id différents pour le même match réel — le regroupement
        # se fait sur le nom normalisé + la date, pas sur match_id.
        signals = [
            _sig("Arsenal vs Chelsea", "h2h", 3.2, match_id="uuid-from-oddsapi"),
            _sig("arsenal vs chelsea", "totals_over", 2.7, match_id="ai_1_abc123"),
        ]
        groups = _group_by_match(signals)
        assert len(groups) == 1
        assert len(groups[0]["legs"]) == 2

    def test_same_fixture_on_another_day_is_another_card(self):
        signals = [
            _sig("Arsenal vs Chelsea", "h2h", 3.2, match_time="2026-07-22T19:00:00+00:00"),
            _sig("Arsenal vs Chelsea", "h2h", 3.2, match_time="2026-07-29T19:00:00+00:00"),
        ]
        assert len(_group_by_match(signals)) == 2


class TestLegIndices:
    def test_leg_idx_points_back_to_the_flat_signal(self):
        signals = [
            _sig("Arsenal vs Chelsea", "h2h", 3.2),
            _sig("Bayern vs Dortmund", "h2h", 2.9, match_id="b"),
            _sig("Arsenal vs Chelsea", "totals_over", 2.7),
        ]
        groups = _group_by_match(signals)
        for g in groups:
            for leg in g["legs"]:
                # C'est cet index que le template passe à openModal().
                assert signals[leg["idx"]] is leg["sig"]

    def test_every_signal_appears_exactly_once(self):
        signals = [
            _sig("Arsenal vs Chelsea", "h2h", 3.2),
            _sig("Arsenal vs Chelsea", "totals_over", 2.7),
            _sig("Bayern vs Dortmund", "spreads_away", 4.1, match_id="b"),
        ]
        idxs = [leg["idx"] for g in _group_by_match(signals) for leg in g["legs"]]
        assert sorted(idxs) == [0, 1, 2]


class TestCardHeadline:
    def test_card_takes_the_best_edge_and_flag_of_its_legs(self):
        signals = [
            _sig("Arsenal vs Chelsea", "totals_over", 2.7, flag="LOW_VALUE"),
            _sig("Arsenal vs Chelsea", "h2h", 6.4, flag="HIGH_VALUE"),
        ]
        g = _group_by_match(signals)[0]
        assert g["best_edge"] == 6.4
        assert g["best_quality"] == 0
        assert g["best_flag"] == "HIGH_VALUE"

    def test_single_leg_card_keeps_its_own_flag(self):
        g = _group_by_match([_sig("Arsenal vs Chelsea", "h2h", 2.0, flag="SUSPECT_DATA")])[0]
        assert g["best_flag"] == "SUSPECT_DATA"
        assert g["best_quality"] == 2

    def test_ordering_of_groups_follows_first_appearance(self):
        # _group_by_match reçoit une liste DÉJÀ triée par _mk_dash_sort ; il ne
        # doit pas réordonner, sinon le tri urgence/sport du serveur saute.
        signals = [
            _sig("Bayern vs Dortmund", "h2h", 2.0, match_id="b"),
            _sig("Arsenal vs Chelsea", "h2h", 9.0),
        ]
        assert [g["match"] for g in _group_by_match(signals)] == \
               ["Bayern vs Dortmund", "Arsenal vs Chelsea"]
