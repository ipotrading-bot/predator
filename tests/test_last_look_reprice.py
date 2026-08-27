"""
tests/test_last_look_reprice.py — le re-contrôle de dernière minute de
`run_engine` : juste avant l'envoi Telegram, on reprixe les legs h2h et on
annule tout système qui n'est plus viable après taxe au prix COURANT.

Depuis le 2026-08-27 le prix reprixé est le prix EXÉCUTABLE — DNB synthétique
en football, cote brute ailleurs — et non plus la cote 1X2 brute. La nuance
n'est pas cosmétique : reprixer un leg dont l'entrée est un DNB synthétique
avec la cote 1X2 brute affichait systématiquement une amélioration de ~10 %
(la marge du book), et le last-look validait des combos que le prix réel
condamne.
"""
from unittest.mock import patch

import core.harvester as harvester
import run_engine
from core.math_engine import synthetic_dnb


def _leg(match, selection_name, sport, market_key, executable_odd, sharp_prob):
    """Leg EN MÉMOIRE — le prix s'y nomme `executable_odd` (cf. _emit)."""
    return {
        "match": match, "selection_name": selection_name, "sport": sport,
        "market_key": market_key, "executable_odd": executable_odd,
        "sharp_prob": sharp_prob, "correlation_group": None,
    }


def _dnb(team_odd, draw_odd):
    """Le prix exécutable qu'un 1X2 brut donne — la référence attendue."""
    return synthetic_dnb(team_odd, draw_odd)


def _system(window, legs, k=None):
    return {"window": window, "legs": legs, "k": k or len(legs), "combined_prob": 0.5,
            "combined_odds": 2.0, "stake": 10.0, "ev": 1.0}


def _fresh_event(home, away, o1, ox, o2):
    return {"home": home, "away": away, "match": f"{home} vs {away}",
            "odds_1xbet": {"1": o1, "X": ox, "2": o2}}


class TestLastLookReprice:
    def test_empty_systems_returns_empty(self):
        assert run_engine._last_look_reprice([], run_engine.log) == []

    def test_price_unchanged_system_survives(self):
        legs = [_leg("Arsenal vs Chelsea", "Arsenal", "soccer", "h2h",
                     _dnb(2.20, 3.4), 0.65)]
        systems = [_system("2026-07-10T20", legs)]

        # 2.20 / nul 3.4 → DNB exécutable 1.5529, soit le prix d'entrée du leg.
        with patch.object(harvester, "_fetch_multi_book",
                          return_value=[_fresh_event("Arsenal", "Chelsea", 2.20, 3.4, 3.2)]):
            result = run_engine._last_look_reprice(systems, run_engine.log)

        assert len(result) == 1
        assert result[0]["legs"][0]["executable_odd"] == _dnb(2.20, 3.4)

    def test_price_worsened_cancels_the_system(self):
        # Entrée : 1X2 brut 2.20 / nul 3.4 → DNB exécutable 1.5529, +EV à
        # true_prob 0.65. Le relevé frais tombe à 1.60 / 3.4 → 1.1294, soit
        # une EV franchement négative : le système doit être annulé.
        legs = [_leg("Arsenal vs Chelsea", "Arsenal", "soccer", "h2h",
                     _dnb(2.20, 3.4), 0.65)]
        systems = [_system("2026-07-10T20", legs)]

        with patch.object(harvester, "_fetch_multi_book",
                          return_value=[_fresh_event("Arsenal", "Chelsea", 1.60, 3.4, 3.2)]):
            result = run_engine._last_look_reprice(systems, run_engine.log)

        assert result == []

    def test_price_improved_system_survives_with_new_price(self):
        legs = [_leg("Arsenal vs Chelsea", "Arsenal", "soccer", "h2h",
                     _dnb(2.20, 3.4), 0.65)]
        systems = [_system("2026-07-10T20", legs)]

        with patch.object(harvester, "_fetch_multi_book",
                          return_value=[_fresh_event("Arsenal", "Chelsea", 2.40, 3.4, 3.2)]):
            result = run_engine._last_look_reprice(systems, run_engine.log)

        assert len(result) == 1
        assert result[0]["legs"][0]["executable_odd"] == _dnb(2.40, 3.4)

    def test_away_selection_uses_the_away_price(self):
        """On reprixe le côté MISÉ, pas le favori du jour : un leg posé sur
        l'extérieur doit être relu sur la cote extérieure, sinon un simple
        basculement de favori écrirait le prix de l'autre équipe."""
        legs = [_leg("Arsenal vs Chelsea", "Chelsea", "soccer", "h2h",
                     _dnb(3.20, 3.4), 0.50)]
        systems = [_system("2026-07-10T20", legs)]

        with patch.object(harvester, "_fetch_multi_book",
                          return_value=[_fresh_event("Arsenal", "Chelsea", 2.0, 3.4, 3.40)]):
            result = run_engine._last_look_reprice(systems, run_engine.log)

        assert len(result) == 1
        assert result[0]["legs"][0]["executable_odd"] == _dnb(3.40, 3.4)

    def test_totals_and_spreads_legs_pass_through_unchanged(self):
        legs = [_leg("Arsenal vs Chelsea", "Over 2.5", "soccer", "totals_over", 2.00, 0.60)]
        systems = [_system("2026-07-10T20", legs)]

        with patch.object(harvester, "_fetch_multi_book",
                          return_value=[_fresh_event("Arsenal", "Chelsea", 1.10, 3.4, 3.2)]):
            result = run_engine._last_look_reprice(systems, run_engine.log)

        # Un leg totals n'est jamais reprixé : c'est sa cote d'origine (encore
        # viable) qui est contrôlée -> le système survit intact.
        assert len(result) == 1
        assert result[0]["legs"][0]["executable_odd"] == 2.00

    def test_fetch_failure_keeps_original_price_not_fatal(self):
        legs = [_leg("Arsenal vs Chelsea", "Arsenal", "soccer", "h2h",
                     _dnb(2.20, 3.4), 0.65)]
        systems = [_system("2026-07-10T20", legs)]

        with patch.object(harvester, "_fetch_multi_book", side_effect=RuntimeError("network down")):
            result = run_engine._last_look_reprice(systems, run_engine.log)

        assert len(result) == 1
        assert result[0]["legs"][0]["executable_odd"] == _dnb(2.20, 3.4)

    def test_no_matching_event_in_fresh_batch_keeps_original(self):
        legs = [_leg("Arsenal vs Chelsea", "Arsenal", "soccer", "h2h",
                     _dnb(2.20, 3.4), 0.65)]
        systems = [_system("2026-07-10T20", legs)]

        with patch.object(harvester, "_fetch_multi_book",
                          return_value=[_fresh_event("Real Madrid", "Barcelona", 1.9, 3.5, 4.0)]):
            result = run_engine._last_look_reprice(systems, run_engine.log)

        assert len(result) == 1
        assert result[0]["legs"][0]["executable_odd"] == _dnb(2.20, 3.4)
