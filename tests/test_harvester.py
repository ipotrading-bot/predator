"""
tests/test_harvester.py — core/harvester.py multi-book line shopping
(Task 6): _fetch_multi_book() must keep the best price per outcome across
every soft source that found the same real-world match, and correctly
attribute provenance via _soft_source. No live HTTP calls — the per-source
fetchers are monkeypatched with synthetic responses.
"""
import core.harvester as harvester


def _match(home, away, o1, ox, o2, league="Test League"):
    return {
        "id": f"{home}_{away}",
        "match": f"{home} vs {away}",
        "home": home,
        "away": away,
        "league": league,
        "sport": "soccer",
        "sport_id": 1,
        "odds_1xbet": {"1": o1, "X": ox, "2": o2},
    }


class TestFuzzyMatchEvent:
    def test_finds_same_match_despite_naming_variance(self):
        pool = [_match("Manchester United", "Chelsea", 2.0, 3.4, 3.6)]
        candidate = _match("Man Utd", "Chelsea", 2.05, 3.3, 3.5)
        found = harvester._fuzzy_match_event(candidate, pool)
        assert found is not None
        assert found["home"] == "Manchester United"

    def test_returns_none_for_unrelated_match(self):
        pool = [_match("Manchester United", "Chelsea", 2.0, 3.4, 3.6)]
        candidate = _match("Real Madrid", "Barcelona", 1.8, 3.8, 4.2)
        assert harvester._fuzzy_match_event(candidate, pool) is None


class TestFetchMultiBook:
    """Line shopping entre les sources soft VIVANTES : odds-api.io et
    titan007 (le LineFeed 1xbet/Melbet/22bet et api-sports ont été retirés le
    2026-09-03 — l'un bloqué par IP, l'autre suspendu deux fois)."""

    def _cabler(self, monkeypatch, odds_api_io=(), titan007=()):
        monkeypatch.setattr(harvester, "_fetch_from_odds_api_io", lambda sid: list(odds_api_io))
        monkeypatch.setattr(harvester, "_t7_fetch", lambda: list(titan007))
        monkeypatch.setattr(harvester, "_measure_consensus", lambda sid, merged: None)

    def test_single_source_passes_through_unchanged(self, monkeypatch):
        self._cabler(monkeypatch, odds_api_io=[_match("Team A", "Team B", 2.0, 3.2, 3.5)])
        result = harvester._fetch_multi_book(1)
        assert len(result) == 1
        assert result[0]["_soft_source"] == "odds_api_io"
        assert result[0]["odds_1xbet"] == {"1": 2.0, "X": 3.2, "2": 3.5}

    def test_keeps_best_price_per_outcome_across_sources(self, monkeypatch):
        self._cabler(monkeypatch,
                     odds_api_io=[_match("Team A", "Team B", 2.00, 3.10, 3.50)],
                     titan007=[_match("Team A", "Team B", 2.10, 3.00, 3.60)])
        result = harvester._fetch_multi_book(1)
        assert len(result) == 1   # same real-world match, not duplicated
        odds = result[0]["odds_1xbet"]
        assert odds["1"] == 2.10   # titan007 had the better price
        assert odds["X"] == 3.10   # odds-api.io had the better price
        assert odds["2"] == 3.60

    def test_soft_source_records_which_sources_contributed(self, monkeypatch):
        self._cabler(monkeypatch,
                     odds_api_io=[_match("Team A", "Team B", 2.00, 3.10, 3.50)],
                     titan007=[_match("Team A", "Team B", 2.10, 3.00, 3.60)])
        result = harvester._fetch_multi_book(1)
        assert set(result[0]["_soft_source"].split("+")) == {"odds_api_io", "titan007"}

    def test_match_unique_to_one_source_still_included(self, monkeypatch):
        # Distinct, realistic team names — placeholders like "Team A"/"Team
        # C" share enough characters ("team ") to false-positive under
        # strict_team_match's fuzzy-ratio fallback.
        self._cabler(monkeypatch,
                     odds_api_io=[_match("Arsenal", "Chelsea", 2.0, 3.2, 3.5)],
                     titan007=[_match("Bayern Munich", "Dortmund", 1.9, 3.3, 4.0)])
        result = harvester._fetch_multi_book(1)
        assert {r["match"] for r in result} == {"Arsenal vs Chelsea", "Bayern Munich vs Dortmund"}

    def test_no_sources_respond_returns_empty(self, monkeypatch):
        self._cabler(monkeypatch)
        assert harvester._fetch_multi_book(1) == []

    def test_worse_price_on_second_source_does_not_override(self, monkeypatch):
        self._cabler(monkeypatch,
                     odds_api_io=[_match("Team A", "Team B", 2.20, 3.10, 3.50)],
                     titan007=[_match("Team A", "Team B", 2.00, 3.00, 3.40)])
        result = harvester._fetch_multi_book(1)
        assert result[0]["odds_1xbet"] == {"1": 2.20, "X": 3.10, "2": 3.50}
        assert result[0]["_soft_source"] == "odds_api_io"

    def test_le_linefeed_est_parti(self):
        """Une source morte laissée en place coûte du budget moteur et fait
        croire à une capacité : plus aucun gabarit d'URL LineFeed ici."""
        import inspect
        src = inspect.getsource(harvester)
        for nom in ("SOFT_BOOKS", "XBET_FEED_TPLS", "_fetch_from_book", "_parse_xbet_json"):
            assert not hasattr(harvester, nom), nom
        assert "LineFeed/Get1x2" not in src


class TestStableId:
    """Les matchs issus de la recherche web recevaient un id positionnel
    (`gemini_{sport}_{i}`). L'ordre du JSON renvoyé par l'IA variant d'un scan
    à l'autre, le même match changeait d'id à chaque tick — le dédoublonnage
    par (match_id, market_key) de run_engine._save() ne mordait jamais et les
    copies s'empilaient — et deux matchs pouvaient se partager un id, auquel
    cas le delete-then-insert frappait le signal d'un autre match."""

    def test_same_match_same_id_regardless_of_position(self):
        a = harvester._stable_id("1", "Arsenal", "Chelsea", "2026-07-22T19:00:00Z")
        b = harvester._stable_id("1", "Arsenal", "Chelsea", "2026-07-22T19:00:00Z")
        assert a == b

    def test_id_survives_casing_and_whitespace_noise(self):
        a = harvester._stable_id("1", "Arsenal", "Chelsea")
        b = harvester._stable_id("1", "  arsenal ", "CHELSEA")
        assert a == b

    def test_different_matches_never_collide(self):
        ids = {
            harvester._stable_id("1", "Arsenal", "Chelsea"),
            harvester._stable_id("1", "Chelsea", "Arsenal"),      # inversé
            harvester._stable_id("1", "Bayern", "Dortmund"),
            harvester._stable_id("4", "Arsenal", "Chelsea"),      # autre sport
        }
        assert len(ids) == 4

    def test_same_fixture_on_another_date_is_another_id(self):
        a = harvester._stable_id("1", "Arsenal", "Chelsea", "2026-07-22T19:00:00Z")
        b = harvester._stable_id("1", "Arsenal", "Chelsea", "2026-07-29T19:00:00Z")
        assert a != b

    def test_time_of_day_does_not_split_the_id(self):
        # Seule la date compte : un léger recalage d'horaire entre deux scans
        # ne doit pas recréer un match "neuf".
        a = harvester._stable_id("1", "Arsenal", "Chelsea", "2026-07-22T19:00:00Z")
        b = harvester._stable_id("1", "Arsenal", "Chelsea", "2026-07-22T20:30:00Z")
        assert a == b
