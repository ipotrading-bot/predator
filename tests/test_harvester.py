"""
tests/test_harvester.py — core/harvester.py multi-book line shopping
(Task 6): _fetch_multi_book() must keep the best price per outcome across
every soft book that found the same real-world match, and correctly
attribute provenance via _soft_source. No live HTTP calls — _fetch_from_book
is monkeypatched with synthetic per-book responses.
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
    def test_single_book_passes_through_unchanged(self, monkeypatch):
        def fake_fetch(book, tpls, referer, sport_id):
            return [_match("Team A", "Team B", 2.0, 3.2, 3.5)] if book == "1xbet" else []

        monkeypatch.setattr(harvester, "_fetch_from_book", fake_fetch)
        result = harvester._fetch_multi_book(1)

        assert len(result) == 1
        assert result[0]["_soft_source"] == "1xbet"
        assert result[0]["odds_1xbet"] == {"1": 2.0, "X": 3.2, "2": 3.5}

    def test_keeps_best_price_per_outcome_across_books(self, monkeypatch):
        def fake_fetch(book, tpls, referer, sport_id):
            if book == "1xbet":
                return [_match("Team A", "Team B", 2.00, 3.10, 3.50)]
            if book == "melbet":
                return [_match("Team A", "Team B", 2.10, 3.00, 3.60)]
            return []

        monkeypatch.setattr(harvester, "_fetch_from_book", fake_fetch)
        result = harvester._fetch_multi_book(1)

        assert len(result) == 1   # same real-world match, not duplicated
        odds = result[0]["odds_1xbet"]
        assert odds["1"] == 2.10   # melbet had the better price
        assert odds["X"] == 3.10   # 1xbet had the better price
        assert odds["2"] == 3.60   # melbet had the better price

    def test_soft_source_records_which_books_contributed(self, monkeypatch):
        def fake_fetch(book, tpls, referer, sport_id):
            if book == "1xbet":
                return [_match("Team A", "Team B", 2.00, 3.10, 3.50)]
            if book == "melbet":
                return [_match("Team A", "Team B", 2.10, 3.00, 3.60)]
            return []

        monkeypatch.setattr(harvester, "_fetch_from_book", fake_fetch)
        result = harvester._fetch_multi_book(1)

        sources = set(result[0]["_soft_source"].split("+"))
        assert sources == {"1xbet", "melbet"}

    def test_match_unique_to_one_book_still_included(self, monkeypatch):
        # Distinct, realistic team names — placeholders like "Team A"/"Team
        # C" share enough characters ("team ") to false-positive under
        # strict_team_match's fuzzy-ratio fallback, which isn't what this
        # test is trying to exercise.
        def fake_fetch(book, tpls, referer, sport_id):
            if book == "1xbet":
                return [_match("Arsenal", "Chelsea", 2.0, 3.2, 3.5)]
            if book == "melbet":
                return [_match("Bayern Munich", "Dortmund", 1.9, 3.3, 4.0)]
            return []

        monkeypatch.setattr(harvester, "_fetch_from_book", fake_fetch)
        result = harvester._fetch_multi_book(1)

        assert len(result) == 2
        matches = {r["match"] for r in result}
        assert matches == {"Arsenal vs Chelsea", "Bayern Munich vs Dortmund"}

    def test_no_books_respond_returns_empty(self, monkeypatch):
        monkeypatch.setattr(harvester, "_fetch_from_book", lambda *a, **k: [])
        assert harvester._fetch_multi_book(1) == []

    def test_worse_price_on_second_book_does_not_override(self, monkeypatch):
        def fake_fetch(book, tpls, referer, sport_id):
            if book == "1xbet":
                return [_match("Team A", "Team B", 2.20, 3.10, 3.50)]
            if book == "melbet":
                return [_match("Team A", "Team B", 2.00, 3.00, 3.40)]
            return []

        monkeypatch.setattr(harvester, "_fetch_from_book", fake_fetch)
        result = harvester._fetch_multi_book(1)

        # 1xbet was strictly better on every outcome -> stays sole source.
        assert result[0]["odds_1xbet"] == {"1": 2.20, "X": 3.10, "2": 3.50}
        assert result[0]["_soft_source"] == "1xbet"
