"""
tests/test_telegram_format.py — format opérateur des messages Telegram.

Demande opérateur 2026-07-21 : les messages ne doivent plus contenir que
l'événement, le favori, la sélection, la cote, l'heure et la valeur. Les
mises Kelly et la bankroll de référence (1000€) sont supprimées — elles
affichaient "Mise 0€ · EV net taxe +0.01€" sur presque chaque ligne, et dans
run_rapport un stake nul faisait carrément DISPARAÎTRE le signal du rapport.

Ces tests verrouillent l'absence de mise/bankroll et la présence des six
champs demandés, sans aucun envoi réseau (_telegram est monkeypatché).
"""
from datetime import datetime, timezone

import pytest

import run_engine
import run_rapport


NOW = datetime(2026, 7, 21, 23, 8, tzinfo=timezone.utc)


def _sig(**over) -> dict:
    base = {
        "match": "Real Madrid vs Barcelona", "sport": "soccer",
        "league": "La Liga", "market_key": "h2h", "market": "AH 0.0",
        "selection_name": "Barcelona", "xbet_odd": 3.40, "sharp_prob": 0.31,
        "edge_pct": 5.4, "risk_flag": "HIGH_VALUE",
        "match_time": "2026-07-21T21:00:00+00:00",
    }
    base.update(over)
    return base


@pytest.fixture
def sent(monkeypatch):
    box: list[str] = []
    monkeypatch.setattr(run_engine, "_telegram", box.append)
    return box


def _system(legs, **over):
    base = {"k": len(legs), "legs": legs, "combined_odds": 3.40,
            "combined_prob": 0.31, "stake": 0.0, "ev": 0.01, "window": "w"}
    base.update(over)
    return base


class TestNoStakeNoBankroll:
    @pytest.mark.parametrize("banned", ["Mise", "1000€", "EV net", "Kelly", "€"])
    def test_engine_message_is_money_free(self, sent, banned):
        run_engine._telegram_systems([_system([_sig()])], NOW, "OVERNIGHT", 46,
                                     "OddsAPI/Pinnacle", 0)
        assert banned not in sent[0]

    @pytest.mark.parametrize("banned", ["Mise", "1000€", "Kelly", "€"])
    def test_report_line_is_money_free(self, banned):
        assert banned not in run_rapport._signal_line(_sig(), NOW)

    def test_zero_stake_signal_is_still_reported(self):
        # Régression : _signal_line renvoyait None quand la mise Kelly tombait
        # sous MIN_STAKE, effaçant le signal du rapport sans rien dire.
        assert run_rapport._signal_line(_sig(xbet_odd=1.01, sharp_prob=0.01), NOW)


class TestRequiredFields:
    def test_event_selection_odds_and_value_are_present(self):
        line = run_rapport._signal_line(_sig(), NOW)
        assert "Real Madrid vs Barcelona" in line   # événement
        assert "Barcelona" in line                  # signal proposé
        assert "3.40" in line                       # cote
        assert "+5.4%" in line                      # valeur
        assert "21:00 UTC" in line                  # heure

    def test_kickoff_shows_date_when_not_today(self):
        line = run_rapport._signal_line(_sig(match_time="2026-07-22T02:30:00+00:00"), NOW)
        assert "22/07 02:30 UTC" in line

    def test_unknown_kickoff_prints_no_hour_rather_than_a_fake_one(self):
        assert "UTC" not in run_rapport._signal_line(_sig(match_time=""), NOW)


class TestFavourite:
    def test_outsider_pick_names_the_favourite(self):
        line = run_rapport._signal_line(_sig(), NOW)
        assert "Favori : Real Madrid" in line

    def test_favourite_pick_is_tagged_inline_not_repeated(self):
        line = run_rapport._signal_line(
            _sig(selection_name="Real Madrid", sharp_prob=0.62), NOW)
        assert "(favori)" in line
        assert "Favori :" not in line

    def test_totals_market_claims_no_favourite(self):
        # Aucun moneyline n'est stocké pour un totals — en nommer un serait
        # l'inventer.
        line = run_rapport._signal_line(
            _sig(market_key="totals_under", selection_name="Under 2.75"), NOW)
        assert "avori" not in line

    def test_engine_and_report_agree(self):
        s = _sig()
        assert run_engine._favourite(s) == run_rapport._favourite(s) == "Real Madrid"


class TestEmptyAndCombo:
    def test_no_systems_message_stays_one_short_line(self, sent):
        run_engine._telegram_systems([], NOW, "EU-CLOSE", 8, "OddsAPI/Pinnacle", 0)
        assert "Aucun pari de valeur · 8 matchs analysés" in sent[0]
        assert "€" not in sent[0]

    def test_combo_shows_combined_odds_and_value(self, sent):
        legs = [_sig(), _sig(match="A vs B", selection_name="A", sharp_prob=0.55)]
        run_engine._telegram_systems([_system(legs, combined_odds=6.46,
                                              combined_prob=0.166)],
                                     NOW, "OVERNIGHT", 46, "OddsAPI/Pinnacle", 0)
        assert "*Combiné* `@ 6.46`" in sent[0]
        assert "+7.2%" in sent[0]          # 6.46 × 0.166 − 1

    def test_single_leg_has_no_combined_line(self, sent):
        run_engine._telegram_systems([_system([_sig()])], NOW, "OVERNIGHT", 46,
                                     "OddsAPI/Pinnacle", 0)
        assert "Combiné" not in sent[0]
