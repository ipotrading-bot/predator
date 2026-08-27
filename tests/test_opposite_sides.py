"""
tests/test_opposite_sides.py — un marché à deux côtés opposés ne doit
produire qu'UN signal.

_process_totals boucle sur over/under et _process_spreads sur home/away.
Sur une ligne Pinnacle symétrique (devig = 50/50), deux cotes soft
suffisamment généreuses franchissent compute_alpha() en même temps — et le
seuil de prob. sharp aussi. Le dashboard affichait alors « Over 2.5 VALEUR »
ET « Under 2.5 VALEUR » sur le même match — deux paris contradictoires, dont
un est forcément un artefact de marge. _keep_best_side() ne garde que le côté
au plus gros edge.

⚠️ RÉGIME DES FIXTURES, resserré par A2 (taxe rétablie à 20 %). Sur un
marché à 50/50, la fenêtre où les DEUX côtés qualifient est désormais étroite
et bornée des deux bords :
  · en bas  : sous une cote de 2,25 l'EV est négative APRÈS taxe, donc la
    mise Kelly vaut 0 et le signal ne sort pas (EV brute +12,5 % au point
    mort) ;
  · en haut : au-delà de +15 % d'EV brute, le garde SUSPECT_EDGE des
    totals/spreads refuse le signal comme donnée douteuse.
D'où les cotes 2,28 / 2,26 (+14 % et +13 % brut). Ce n'est pas un réglage de
confort : c'est la mesure de ce qu'un marché quasi pile-ou-face devient une
fois le coût de transaction payé. Un pari à 50/50 n'est plus jouable qu'entre
2,25 et 2,30 — A6 aura à en tirer les conséquences.
"""
import logging
from datetime import datetime, timedelta, timezone

import run_engine

log = logging.getLogger("test")


def _now():
    return datetime.now(timezone.utc)


def _kickoff(hours=3):
    return (_now() + timedelta(hours=hours)).isoformat()


def _totals_match(x_over, x_under, p_over=1.90, p_under=1.90):
    return {
        "id": "match-1",
        "commence_time": _kickoff(),
        "totals_1xbet":    {"over": x_over, "under": x_under, "point": 2.5},
        "totals_pinnacle": {"over": p_over, "under": p_under, "point": 2.5},
    }


def _spreads_match(x_home, x_away, p_home=1.90, p_away=1.90):
    return {
        "id": "match-2",
        "commence_time": _kickoff(),
        "spreads_1xbet":    {"home": x_home, "away": x_away, "point": -1.5},
        "spreads_pinnacle": {"home": p_home, "away": p_away, "point": -1.5},
    }


def _run_totals(m):
    out = []
    run_engine._process_totals(m, "Arsenal vs Chelsea", "soccer", "PL", "⚽",
                               out, None, _now(), log, min_edge=1.0)
    return out


def _run_spreads(m):
    out = []
    run_engine._process_spreads(m, "Arsenal vs Chelsea", "soccer", "PL",
                                "Arsenal", "Chelsea", "⚽",
                                out, None, _now(), log, min_edge=1.0)
    return out


class TestTotals:
    def test_both_sides_positive_keeps_only_the_best(self):
        # Pinnacle symétrique 1.90/1.90 (devig = 50/50, les deux côtés passent
        # le seuil de prob.), Melbet au-dessus du breakeven des deux bords.
        signals = _run_totals(_totals_match(x_over=2.28, x_under=2.26))
        assert len(signals) == 1
        assert signals[0]["market_key"] == "totals_over"     # EV +14% > +13%
        assert signals[0]["selection_name"] == "Over 2.5"

    def test_best_side_is_edge_based_not_order_based(self):
        # Under gagne cette fois : la boucle voit toujours "over" en premier,
        # le tri doit se faire sur l'edge, pas sur l'ordre d'itération.
        signals = _run_totals(_totals_match(x_over=2.26, x_under=2.28))
        assert len(signals) == 1
        assert signals[0]["market_key"] == "totals_under"

    def test_single_qualifying_side_still_emitted(self):
        # Non-régression : un seul côté au-dessus du seuil doit continuer à
        # sortir normalement.
        signals = _run_totals(_totals_match(x_over=2.28, x_under=1.80))
        assert len(signals) == 1
        assert signals[0]["market_key"] == "totals_over"

    def test_no_qualifying_side_emits_nothing(self):
        assert _run_totals(_totals_match(x_over=1.80, x_under=1.80)) == []


class TestSpreads:
    def test_both_sides_positive_keeps_only_the_best(self):
        signals = _run_spreads(_spreads_match(x_home=2.28, x_away=2.26))
        assert len(signals) == 1
        assert signals[0]["market_key"] == "spreads_home"

    def test_best_side_is_edge_based_not_order_based(self):
        signals = _run_spreads(_spreads_match(x_home=2.26, x_away=2.28))
        assert len(signals) == 1
        assert signals[0]["market_key"] == "spreads_away"

    def test_single_qualifying_side_still_emitted(self):
        signals = _run_spreads(_spreads_match(x_home=1.80, x_away=2.28))
        assert len(signals) == 1
        assert signals[0]["market_key"] == "spreads_away"
