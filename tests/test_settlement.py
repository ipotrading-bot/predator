"""
tests/test_settlement.py — core/settlement.py real-score parsing +
core/ai_search.py's search/extraction fallback chain.

2026-07-21 : le transport Gemini a été remplacé par core/ai_search.py
(groq/compound-mini avec recherche web intégrée → fallback Tavily +
llama-3.3-70b). Les tests de parsing pinnent les mêmes modes de panne
qu'avant (JSON en fence markdown, réponse tronquée, vide) contre le
NOUVEAU transport — settlement.fetch_match_result parse toujours du
texte brut retourné par ai_search_complete.

No live HTTP calls — the transport (ai_search_complete / requests.post)
is monkeypatched with synthetic responses.
"""
import json

import pytest

import core.ai_search as ai_search
import core.settlement as settlement


class _FakeResponse:
    def __init__(self, status_code, json_data=None, text=""):
        self.status_code = status_code
        self._json_data = json_data
        self.text = text if text else (json.dumps(json_data) if json_data is not None else "")

    def json(self):
        return self._json_data


def _groq_body(text):
    """Shape of a real Groq (OpenAI-compatible) chat completion response."""
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


class TestFetchMatchResult:
    @pytest.fixture(autouse=True)
    def _fake_key(self, monkeypatch):
        # Sandbox has no real Groq credentials — fetch_match_result bails
        # out at the top with `if not ai_available(): return None` otherwise,
        # which would make every test below pass for the wrong reason.
        monkeypatch.setenv("GROQ_API_KEY", "gsk-fake-key-for-tests")
        # Reset the module-level daily-dead flag between tests.
        monkeypatch.setattr(ai_search, "_groq_daily_dead", False)

    def _patch_ai(self, monkeypatch, text):
        monkeypatch.setattr(
            settlement, "ai_search_complete",
            lambda prompt, queries, label, **kw: text,
        )

    def test_parses_completed_match(self, monkeypatch):
        self._patch_ai(monkeypatch, '{"completed":true,"home_score":6,"away_score":2}')
        result = settlement.fetch_match_result("Boston Red Sox vs New York Mets", "baseball", "2026-07-10")
        assert result == {"home_score": 6, "away_score": 2, "completed": True}

    def test_parses_json_wrapped_in_markdown_fence(self, monkeypatch):
        self._patch_ai(monkeypatch, '```json\n{"completed":true,"home_score":1,"away_score":0}\n```')
        result = settlement.fetch_match_result("Spain vs Belgium", "soccer", "2026-07-10")
        assert result == {"home_score": 1, "away_score": 0, "completed": True}

    def test_not_completed_returns_none(self, monkeypatch):
        self._patch_ai(monkeypatch, '{"completed":false}')
        result = settlement.fetch_match_result("Team A vs Team B", "soccer", "2026-07-10")
        assert result is None

    def test_truncated_response_logs_diagnostic_and_returns_none(self, monkeypatch, caplog):
        """Réponse coupée avant le '}' final (l'ancien mode de panne
        maxOutputTokens=80) : ne doit pas lever, doit retourner None, et
        doit logger de quoi diagnostiquer (pas un `return None` muet)."""
        self._patch_ai(monkeypatch, '{"completed":true,"home_score":6,"awa')
        with caplog.at_level("WARNING"):
            result = settlement.fetch_match_result("Boston Red Sox vs New York Mets", "baseball", "2026-07-10")
        assert result is None
        assert any("no-JSON" in r.message for r in caplog.records)

    def test_transport_failure_returns_none(self, monkeypatch):
        self._patch_ai(monkeypatch, None)
        result = settlement.fetch_match_result("Team A vs Team B", "soccer", "2026-07-10")
        assert result is None

    def test_no_groq_key_returns_none_without_calling_transport(self, monkeypatch):
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        called = []
        monkeypatch.setattr(
            settlement, "ai_search_complete",
            lambda *a, **kw: called.append(1) or "{}",
        )
        result = settlement.fetch_match_result("Team A vs Team B", "soccer", "2026-07-10")
        assert result is None
        assert not called


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


class TestAiSearchFallbackChain:
    """core/ai_search.py : compound-mini d'abord, Tavily+llama en secours,
    court-circuit total une fois le quota journalier Groq mort."""

    @pytest.fixture(autouse=True)
    def _keys(self, monkeypatch):
        monkeypatch.setenv("GROQ_API_KEY", "gsk-fake")
        monkeypatch.setenv("TAVILY_API_KEY", "tvly-fake")
        monkeypatch.setattr(ai_search, "_groq_daily_dead", False)
        monkeypatch.setattr(ai_search, "_tavily_used", 0)

    def test_compound_mini_success_never_hits_tavily(self, monkeypatch):
        calls = []

        def fake_post(url, json=None, headers=None, timeout=None):
            calls.append(url)
            assert "groq.com" in url
            return _FakeResponse(200, _groq_body('[{"match":"A vs B"}]'))

        monkeypatch.setattr(ai_search.requests, "post", fake_post)
        text = ai_search.ai_search_complete("prompt", ["query"], label="test")
        assert text == '[{"match":"A vs B"}]'
        assert len(calls) == 1

    def test_compound_failure_falls_back_to_tavily_plus_llama(self, monkeypatch):
        calls = []

        def fake_post(url, json=None, headers=None, timeout=None):
            calls.append(url)
            if "groq.com" in url and json["model"] == ai_search._SEARCH_MODEL:
                return _FakeResponse(500, text="oops")
            if "tavily.com" in url:
                return _FakeResponse(200, {"results": [
                    {"title": "T", "url": "u", "content": "snippet"}]})
            return _FakeResponse(200, _groq_body('{"ok":true}'))

        monkeypatch.setattr(ai_search.requests, "post", fake_post)
        text = ai_search.ai_search_complete("prompt", ["query"], label="test")
        assert text == '{"ok":true}'
        assert any("tavily.com" in c for c in calls)

    def test_daily_quota_dead_short_circuits_everything(self, monkeypatch):
        calls = []

        def fake_post(url, json=None, headers=None, timeout=None):
            calls.append(url)
            return _FakeResponse(
                429, text='{"error":{"message":"Rate limit reached ... per day (TPD)"}}')

        monkeypatch.setattr(ai_search.requests, "post", fake_post)
        first = ai_search.ai_search_complete("prompt", ["query"], label="test")
        assert first is None
        assert ai_search.ai_dead()
        n = len(calls)
        second = ai_search.ai_search_complete("prompt2", ["query2"], label="test")
        assert second is None
        assert len(calls) == n   # zero new HTTP calls once dead

    def test_tavily_run_budget_respected(self, monkeypatch):
        monkeypatch.setattr(ai_search, "_TAVILY_RUN_BUDGET", 1)
        calls = []

        def fake_post(url, json=None, headers=None, timeout=None):
            calls.append(url)
            return _FakeResponse(200, {"results": [
                {"title": "T", "url": "u", "content": "s"}]})

        monkeypatch.setattr(ai_search.requests, "post", fake_post)
        assert ai_search.tavily_search("q1") != []
        assert ai_search.tavily_search("q2") == []   # budget épuisé
        assert len([c for c in calls if "tavily" in c]) == 1
