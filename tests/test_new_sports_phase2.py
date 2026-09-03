"""
tests/test_new_sports_phase2.py — Phase 2 du recentrage sports (2026-08-22).

NFL (saison régulière seulement), Ligue des Champions, Europa League et
Euroleague entrent dans SPORT_KEYS AVANT leur saison : le pré-vol gratuit
rend 0 tant qu'il n'y a rien à scanner, donc l'ajout ne coûte rien. La NFL
porte en plus un garde de date (SEASON_OPENS) : la présaison d'août est
exactement ce qu'on ne veut pas, et le pré-vol ne la distingue pas.
"""
from datetime import datetime, timezone


import run_engine
from core import odds_api
from core.constants import KELLY_FRACTION
from core.learning_layer import SPORT_DEFAULTS
from core.odds_api import SPORT_KEYS, _MARKETS_BY_SPORT, _season_open


class TestWiring:
    def test_keys_and_sport_types(self):
        assert SPORT_KEYS["americanfootball_nfl"] == "americanfootball"
        assert SPORT_KEYS["soccer_uefa_champs_league"] == "soccer"
        assert SPORT_KEYS["soccer_uefa_europa_league"] == "soccer"
        assert SPORT_KEYS["basketball_euroleague"] == "euroleague_basketball"

    def test_kelly_fractions(self):
        assert KELLY_FRACTION["americanfootball"] == 0.14
        assert KELLY_FRACTION["euroleague_basketball"] == 0.12
        assert KELLY_FRACTION["euroleague_basketball"] < KELLY_FRACTION["basketball"]

    def test_euroleague_mirrors_basketball_mechanics(self):
        assert _MARKETS_BY_SPORT["euroleague_basketball"] == _MARKETS_BY_SPORT["basketball"]
        from core.paim_engine import _CONSENSUS_WEIGHTS
        assert _CONSENSUS_WEIGHTS["euroleague_basketball"] == _CONSENSUS_WEIGHTS["basketball"]
        assert "euroleague_basketball" in run_engine._MAJOR_SPORTS   # cap SUSPECT appliqué
        assert "americanfootball" in run_engine._MAJOR_SPORTS

    def test_no_active_sport_was_removed(self):
        for key in ("baseball_mlb", "baseball_kbo", "baseball_npb",
                    "soccer_brazil_campeonato", "soccer_usa_mls",
                    "soccer_argentina_primera_division", "soccer_mexico_ligamx",
                    "soccer_conmebol_copa_libertadores", "aussierules_afl",
                    "rugbyleague_nrl", "basketball_wnba", "soccer_epl",
                    "soccer_spain_la_liga", "soccer_germany_bundesliga",
                    "soccer_italy_serie_a", "soccer_france_ligue_one"):
            assert key in SPORT_KEYS, key

    def test_four_file_invariant(self):
        served = set(SPORT_KEYS.values())
        assert served <= set(KELLY_FRACTION), served - set(KELLY_FRACTION)
        assert served <= set(SPORT_DEFAULTS), served - set(SPORT_DEFAULTS)
        assert served <= set(run_engine.SPORT_QUOTA), served - set(run_engine.SPORT_QUOTA)
        assert served <= set(run_engine.SPORT_EMOJI)


class TestSeasonGate:
    def test_nfl_closed_before_opening_open_after(self):
        before = datetime(2026, 8, 25, tzinfo=timezone.utc)
        after  = datetime(2026, 9, 12, tzinfo=timezone.utc)
        assert _season_open("americanfootball_nfl", before) is False
        assert _season_open("americanfootball_nfl", after) is True

    def test_leagues_without_a_date_are_always_open(self):
        assert _season_open("soccer_epl", datetime(2000, 1, 1, tzinfo=timezone.utc)) is True

    def test_unreadable_date_never_blocks(self, monkeypatch):
        monkeypatch.setitem(odds_api.SEASON_OPENS, "americanfootball_nfl", "n/a")
        assert _season_open("americanfootball_nfl", datetime.now(timezone.utc)) is True

    def test_closed_season_makes_zero_calls_not_even_the_preflight(self, monkeypatch):
        calls = []

        def fake_get(url, params=None, timeout=None):
            if url.rstrip("/").endswith("/sports"):
                class _R:
                    status_code = 200
                    headers = {"x-requests-remaining": "400", "x-requests-used": "1"}
                    def json(self): return []
                return _R()
            calls.append(url)
            raise AssertionError("aucun appel attendu hors saison")

        monkeypatch.setattr(odds_api.requests, "get", fake_get)
        monkeypatch.setitem(odds_api.SEASON_OPENS, "americanfootball_nfl", "2999-01-01")
        odds_api.fetch_odds(api_key="k", hours_ahead=24,
                            sport_keys={"americanfootball_nfl": "americanfootball"})
        assert calls == []
