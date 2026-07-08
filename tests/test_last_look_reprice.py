"""
tests/test_last_look_reprice.py — run_engine.py's Task 8 last-look
re-check: right before Telegram send, re-fetch h2h soft-book prices and
cancel any system that's no longer tax-viable at current prices.
"""
from unittest.mock import patch

import core.harvester as harvester
import run_engine


def _leg(match, selection_name, sport, market_key, xbet_odd, sharp_prob):
    return {
        "match": match, "selection_name": selection_name, "sport": sport,
        "market_key": market_key, "xbet_odd": xbet_odd, "sharp_prob": sharp_prob,
        "correlation_group": None,
    }


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
        legs = [_leg("Arsenal vs Chelsea", "Arsenal", "soccer", "h2h", 2.20, 0.65)]
        systems = [_system("2026-07-10T20", legs)]

        with patch.object(harvester, "_fetch_multi_book",
                          return_value=[_fresh_event("Arsenal", "Chelsea", 2.20, 3.4, 3.2)]):
            result = run_engine._last_look_reprice(systems, run_engine.log)

        assert len(result) == 1
        assert result[0]["legs"][0]["xbet_odd"] == 2.20

    def test_price_worsened_cancels_the_system(self):
        # Scan-time odd 2.20 at true_prob 0.65 is comfortably +EV; a fresh
        # price of 1.40 on the same true_prob should no longer clear tax.
        legs = [_leg("Arsenal vs Chelsea", "Arsenal", "soccer", "h2h", 2.20, 0.65)]
        systems = [_system("2026-07-10T20", legs)]

        with patch.object(harvester, "_fetch_multi_book",
                          return_value=[_fresh_event("Arsenal", "Chelsea", 1.40, 3.4, 3.2)]):
            result = run_engine._last_look_reprice(systems, run_engine.log)

        assert result == []

    def test_price_improved_system_survives_with_new_price(self):
        legs = [_leg("Arsenal vs Chelsea", "Arsenal", "soccer", "h2h", 2.20, 0.65)]
        systems = [_system("2026-07-10T20", legs)]

        with patch.object(harvester, "_fetch_multi_book",
                          return_value=[_fresh_event("Arsenal", "Chelsea", 2.40, 3.4, 3.2)]):
            result = run_engine._last_look_reprice(systems, run_engine.log)

        assert len(result) == 1
        assert result[0]["legs"][0]["xbet_odd"] == 2.40

    def test_away_selection_uses_the_away_price(self):
        legs = [_leg("Arsenal vs Chelsea", "Chelsea", "soccer", "h2h", 3.20, 0.40)]
        systems = [_system("2026-07-10T20", legs)]

        with patch.object(harvester, "_fetch_multi_book",
                          return_value=[_fresh_event("Arsenal", "Chelsea", 2.0, 3.4, 3.40)]):
            result = run_engine._last_look_reprice(systems, run_engine.log)

        assert len(result) == 1
        assert result[0]["legs"][0]["xbet_odd"] == 3.40

    def test_totals_and_spreads_legs_pass_through_unchanged(self):
        legs = [_leg("Arsenal vs Chelsea", "Over 2.5", "soccer", "totals_over", 2.00, 0.60)]
        systems = [_system("2026-07-10T20", legs)]

        with patch.object(harvester, "_fetch_multi_book",
                          return_value=[_fresh_event("Arsenal", "Chelsea", 1.10, 3.4, 3.2)]):
            result = run_engine._last_look_reprice(systems, run_engine.log)

        # totals leg is never repriced, so its original (still-viable) odd
        # is what's checked -> system survives untouched.
        assert len(result) == 1
        assert result[0]["legs"][0]["xbet_odd"] == 2.00

    def test_fetch_failure_keeps_original_price_not_fatal(self):
        legs = [_leg("Arsenal vs Chelsea", "Arsenal", "soccer", "h2h", 2.20, 0.65)]
        systems = [_system("2026-07-10T20", legs)]

        with patch.object(harvester, "_fetch_multi_book", side_effect=RuntimeError("network down")):
            result = run_engine._last_look_reprice(systems, run_engine.log)

        assert len(result) == 1
        assert result[0]["legs"][0]["xbet_odd"] == 2.20

    def test_no_matching_event_in_fresh_batch_keeps_original(self):
        legs = [_leg("Arsenal vs Chelsea", "Arsenal", "soccer", "h2h", 2.20, 0.65)]
        systems = [_system("2026-07-10T20", legs)]

        with patch.object(harvester, "_fetch_multi_book",
                          return_value=[_fresh_event("Real Madrid", "Barcelona", 1.9, 3.5, 4.0)]):
            result = run_engine._last_look_reprice(systems, run_engine.log)

        assert len(result) == 1
        assert result[0]["legs"][0]["xbet_odd"] == 2.20
