"""
tests/test_retired_sports.py — Phase 0 du recentrage sports (2026-08-22).

eSports, tennis de table, volleyball et handball sont RETIRÉS : leur prix de
référence venait d'une recherche web IA, jamais d'un book sharp — du bruit.
Trois choses doivent rester vraies :
1. le moteur ne génère plus JAMAIS de signal pour ces sports, même si un
   cache meta résiduel, un slate REPRICE ou un harvest tiers en ramène ;
2. les fonctions de collecte et les cartes associées ont disparu du code ;
3. le settlement des lignes HISTORIQUES continue : aucune donnée n'est
   supprimée, les lignes restent lisibles par la couche d'apprentissage.
"""
import inspect
import logging
from datetime import datetime, timedelta, timezone

import pytest

import run_engine
from core import constants, harvester, learning_layer, matchbook
from core.constants import KELLY_FRACTION, RETIRED_SPORTS

log = logging.getLogger("test")
RETIRED = ("esports", "tabletennis", "volleyball", "handball")


def _kickoff(hours=3):
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


class TestNoSignalEver:
    @pytest.mark.parametrize("sport", RETIRED)
    def test_emit_refuses_even_with_a_great_edge(self, sport):
        signals = []
        # EV +6.4%, worst-case +4.5%, Kelly > 0 : tout passerait pour un sport vivant.
        run_engine._emit(signals, None, datetime.now(timezone.utc), log,
                         "A vs B", sport, "L", "h2h", "ML", 1.90, 1.80, 0.56, "🎯",
                         selection_name="A", min_edge=1.5, match_time=_kickoff(),
                         match_id="x1", sharp_prob_cons=0.55)
        assert signals == []

    @pytest.mark.parametrize("sport", RETIRED)
    def test_process_h2h_emits_nothing(self, sport):
        out = []
        m = {"id": "m1", "commence_time": _kickoff(),
             "home": "Alpha", "away": "Beta",
             "odds_1xbet":    {"1": 1.95, "X": 0.0, "2": 1.95},
             "odds_pinnacle": {"1": 1.80, "X": 0.0, "2": 2.05}}
        run_engine._process_h2h(m, "Alpha vs Beta", sport, "L", "Alpha", "Beta",
                                "🎯", out, None, datetime.now(timezone.utc), log,
                                min_edge=1.5)
        assert out == []

    def test_a_live_sport_with_the_same_numbers_still_emits(self):
        # Témoin : le garde ne mord que sur RETIRED_SPORTS.
        signals = []
        run_engine._emit(signals, None, datetime.now(timezone.utc), log,
                         "A vs B", "basketball", "L", "h2h", "ML", 1.90, 1.80, 0.56,
                         "🏀", selection_name="A", min_edge=1.5,
                         match_time=_kickoff(), match_id="x2", sharp_prob_cons=0.55)
        assert len(signals) == 1


class TestCodeRemoved:
    def test_fetchers_are_gone(self):
        assert not hasattr(harvester, "fetch_esports_events")
        assert not hasattr(harvester, "fetch_alternative_sports_batch")
        assert not hasattr(harvester, "ALT_SEARCH_MAX_TOKENS")
        assert not hasattr(run_engine, "fetch_esports_events")
        assert not hasattr(run_engine, "fetch_alternative_sports_batch")

    def test_sport_maps_no_longer_list_them(self):
        for sport in RETIRED:
            assert sport not in KELLY_FRACTION
            assert sport not in learning_layer.SPORT_DEFAULTS
            assert sport not in run_engine.SPORT_EMOJI
            assert sport not in run_engine._NO_ODDSAPI_SPORTS
            assert sport not in matchbook.SPORT_IDS
        assert set(RETIRED) == set(RETIRED_SPORTS)

    def test_the_four_file_sport_key_invariant_holds(self):
        # Tout sport-type servi par OddsAPI doit être connu des cartes
        # Kelly / seuils ; les retirés ne doivent apparaître nulle part.
        from core.odds_api import SPORT_KEYS
        served = set(SPORT_KEYS.values())
        assert served & set(RETIRED) == set()
        assert served <= set(KELLY_FRACTION), served - set(KELLY_FRACTION)
        assert served <= set(learning_layer.SPORT_DEFAULTS), \
            served - set(learning_layer.SPORT_DEFAULTS)


class TestHistoryStillSettles:
    def test_settlement_never_filters_on_sport(self):
        # Le retrait passe par le scan, pas par le règlement : aucune référence
        # à RETIRED_SPORTS dans la chaîne settlement/audit/ledger.
        from core import settlement, audit_engine, db
        for mod in (settlement, audit_engine, db):
            assert "RETIRED_SPORTS" not in inspect.getsource(mod)

    def test_determine_outcome_works_for_a_retired_sport_row(self):
        from core.settlement import determine_outcome
        # Une ligne historique tabletennis h2h se règle comme n'importe quel ML.
        out = determine_outcome("tabletennis", "h2h", "Ma Long",
                                "Ma Long", "Fan Zhendong", 4, 2)
        assert out == "WIN"

    def test_learning_layer_reads_historical_rows_without_error(self):
        rows = [{"outcome": "WIN", "kelly_pct": 0.5, "odds": 1.8,
                 "market_type": "h2h", "initial_edge": 3.0, "sharp_prob": 0.6,
                 "clv_pct_real": None, "time_to_match_minutes": 300}] * 5
        # playable_rows ne connaît pas le sport — une ligne esports passe.
        assert len(learning_layer.playable_rows(rows)) == 5

    def test_constant_is_documented_as_data_preserving(self):
        src = inspect.getsource(constants)
        assert "RETIRED_SPORTS" in src and "CONSERVÉES" in src
