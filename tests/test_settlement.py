"""
tests/test_settlement.py — core/settlement.py : chaîne de scores DÉTERMINISTE
et calcul d'issue (determine_outcome).

2026-09-02 : la recherche web (Groq compound-mini + Tavily) a été SUPPRIMÉE du
settlement — le score vient de core/score_sources (MLB statsapi, ESPN,
TheSportsDB) ; api-sports, premier étage jusqu'au 2026-09-03, est retiré. Les anciennes classes TestAiSearchFallbackChain et
TestGroqKeyRotation sont parties avec le transport qu'elles testaient ; la
chaîne de repli testée ici est celle des SOURCES STRUCTURÉES.

Aucun appel HTTP réel — les deux étages sont monkeypatchés.
"""
import core.settlement as settlement


class TestFetchMatchResult:
    """La chaîne : core/score_sources.fetch_score, et rien d'autre. None =
    « pas trouvé aujourd'hui », jamais un état terminal. api-sports en était
    le premier étage jusqu'au 2026-09-03 (deux comptes suspendus, retiré)."""

    def test_score_sources_is_the_whole_chain(self, monkeypatch):
        monkeypatch.setattr(settlement, "fetch_score",
                            lambda *a, **k: {"home_score": 0, "away_score": 3,
                                        "completed": True, "source": "thesportsdb"})
        res = settlement.fetch_match_result("Hapoel Acre vs Bnei Yehuda", "soccer", "2026-08-31")
        assert res == {"home_score": 0, "away_score": 3,
                       "completed": True, "source": "thesportsdb"}

    def test_nothing_found_returns_none(self, monkeypatch):
        monkeypatch.setattr(settlement, "fetch_score", lambda *a, **k: None)
        assert settlement.fetch_match_result("A vs B", "soccer", "2026-09-01") is None

    def test_api_sports_est_parti(self):
        """Décision opérateur 2026-09-03 : « vivre sans api-football ». Une
        source morte laissée en place coûte du budget et fait croire à une
        capacité (leçon LineFeed) : plus aucune trace ici."""
        import inspect
        src = inspect.getsource(settlement)
        for absent in ("api_sports", "result_from_api_sports", "fetch_results"):
            assert absent not in src.replace("api-sports en était", "").replace("core/api_sports", "")

    def test_no_ai_layer_involved(self):
        """Gardien de la suppression : le module settlement n'importe plus
        rien de core.ai_search — un score ne passe JAMAIS par un LLM."""
        import inspect
        src = inspect.getsource(settlement)
        assert "ai_search" not in src
        assert "ai_search_complete" not in src


class TestDetermineOutcome:
    def test_soccer_h2h_draw_is_push(self):
        assert settlement.determine_outcome("soccer", "h2h", "Spain", "Spain", "Belgium", 1, 1) == "PUSH"

    def test_soccer_h2h_home_win(self):
        assert settlement.determine_outcome("soccer", "h2h", "Spain", "Spain", "Belgium", 2, 1) == "WIN"

    def test_soccer_h2h_home_selection_loses(self):
        assert settlement.determine_outcome("soccer", "h2h", "Spain", "Spain", "Belgium", 0, 2) == "LOSS"

    def test_soccer_h2h_away_selection_wins(self):
        assert settlement.determine_outcome("soccer", "h2h", "Belgium", "Spain", "Belgium", 0, 2) == "WIN"

    def test_basketball_h2h_no_push_on_tie_path(self):
        # Non-soccer h2h has no draw handling — moneyline can't tie in practice.
        assert settlement.determine_outcome("basketball", "h2h", "Lakers", "Lakers", "Celtics", 110, 108) == "WIN"

    def test_totals_over_wins(self):
        outcome = settlement.determine_outcome("baseball", "totals", "Over 9.5", "Mets", "Red Sox", 6, 4)
        assert outcome == "WIN"

    def test_totals_under_wins(self):
        outcome = settlement.determine_outcome("baseball", "totals", "Under 9.5", "Mets", "Red Sox", 3, 2)
        assert outcome == "WIN"

    def test_totals_push_on_exact_line(self):
        outcome = settlement.determine_outcome("baseball", "totals", "Over 9.0", "Mets", "Red Sox", 5, 4)
        assert outcome == "PUSH"

    def test_totals_over_loses_when_under_line(self):
        outcome = settlement.determine_outcome("baseball", "totals", "Over 9.5", "Mets", "Red Sox", 2, 1)
        assert outcome == "LOSS"

    def test_spreads_home_covers(self):
        # Home favored by -6 (selection "PS -6.0"): home_score + (-6) > away_score
        outcome = settlement.determine_outcome(
            "basketball", "spreads_home", "PS -6.0", "Lakers", "Celtics", 110, 100)
        assert outcome == "WIN"

    def test_spreads_home_fails_to_cover(self):
        outcome = settlement.determine_outcome(
            "basketball", "spreads_home", "PS -6.0", "Lakers", "Celtics", 105, 100)
        assert outcome == "LOSS"

    def test_spreads_push_on_exact_line(self):
        outcome = settlement.determine_outcome(
            "basketball", "spreads_home", "PS -6.0", "Lakers", "Celtics", 106, 100)
        assert outcome == "PUSH"

    def test_unparseable_selection_returns_unknown(self):
        outcome = settlement.determine_outcome("baseball", "totals", "no number here", "Mets", "Red Sox", 5, 4)
        assert outcome == "UNKNOWN"

    def test_shared_token_teams_exact_selection_still_resolves(self):
        outcome = settlement.determine_outcome(
            "soccer", "h2h", "America MG", "America MG", "America RN", 2, 1)
        assert outcome == "WIN"

    def test_shared_token_away_selection_does_not_default_to_home(self):
        outcome = settlement.determine_outcome(
            "soccer", "h2h", "America RN", "America MG", "America RN", 1, 2)
        assert outcome == "WIN"   # away won 2-1

    def test_ambiguous_selection_matching_both_teams_returns_unknown(self):
        # A selection that fuzzy-matches BOTH shared-token teams must never
        # be guessed — refusing to grade beats a coin-flip WIN/LOSS in the ledger.
        outcome = settlement.determine_outcome(
            "soccer", "h2h", "America", "America MG", "America RN", 2, 1)
        assert outcome == "UNKNOWN"

    def test_selection_matching_neither_team_returns_unknown(self):
        outcome = settlement.determine_outcome(
            "soccer", "h2h", "Real Madrid", "America MG", "America RN", 2, 1)
        assert outcome == "UNKNOWN"

    def test_basketball_h2h_ambiguous_selection_returns_unknown(self):
        outcome = settlement.determine_outcome(
            "basketball", "h2h", "Miami", "Miami Heat", "Miami Hurricanes", 100, 90)
        assert outcome == "UNKNOWN"
