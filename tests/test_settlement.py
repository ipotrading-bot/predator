"""
tests/test_settlement.py — core/settlement.py real-score parsing +
core/http_utils.py's per-project model fallback.

2026-07-11: settlement.py's maxOutputTokens was 80 while every other
search-grounded call site in this codebase uses 200-3000. A live audit
run settled 0/10 signals overnight across MLB, WNBA, NPB, and a World
Cup match — including two independently verified via web search to have
been decided hours earlier (Red Sox 6-2 Mets, Valkyries 79-64 Sun) — a
near-100% miss rate that isn't plausible as genuine "not found" search
misses. These tests pin down the parsing behavior with response shapes
representative of BOTH the old truncated-at-80-tokens failure mode and
a properly-sized response, plus the new per-project model fallback, so
a regression back to either bug fails CI instead of silently
reappearing three weeks from now.

No live HTTP calls — requests.post is monkeypatched with synthetic
responses shaped like real Gemini API payloads.
"""
import json

import pytest

import core.http_utils as http_utils
import core.settlement as settlement


class _FakeResponse:
    def __init__(self, status_code, json_data=None, text=""):
        self.status_code = status_code
        self._json_data = json_data
        self.text = text if text else (json.dumps(json_data) if json_data is not None else "")

    def json(self):
        return self._json_data


def _grounded_candidate(text, finish_reason="STOP"):
    """Shape of a real Gemini generateContent response with google_search
    grounding enabled — includes groundingMetadata, which is why a tight
    maxOutputTokens budget is riskier here than for a plain text call."""
    return {
        "candidates": [{
            "content": {"role": "model", "parts": [{"text": text}]},
            "finishReason": finish_reason,
            "groundingMetadata": {"webSearchQueries": ["some query"]},
        }]
    }


class TestFetchMatchResult:
    @pytest.fixture(autouse=True)
    def _fake_key(self, monkeypatch):
        # Sandbox has no real Gemini credentials — fetch_match_result bails
        # out at the top with `if not api_key: return None` otherwise, which
        # would make every test below pass for the wrong reason (None
        # because of a missing key, not because of the behavior under test).
        monkeypatch.setenv("GEMINI_API_KEY_AUDIT", "fake-key-for-tests")

    def test_parses_completed_match_from_realistic_response(self, monkeypatch):
        body = _grounded_candidate('{"completed":true,"home_score":6,"away_score":2}')
        monkeypatch.setattr(
            http_utils.requests, "post",
            lambda url, json, timeout: _FakeResponse(200, body),
        )
        result = settlement.fetch_match_result("Boston Red Sox vs New York Mets", "baseball", "2026-07-10")
        assert result == {"home_score": 6, "away_score": 2, "completed": True}

    def test_parses_json_wrapped_in_markdown_fence(self, monkeypatch):
        body = _grounded_candidate('```json\n{"completed":true,"home_score":1,"away_score":0}\n```')
        monkeypatch.setattr(
            http_utils.requests, "post",
            lambda url, json, timeout: _FakeResponse(200, body),
        )
        result = settlement.fetch_match_result("Spain vs Belgium", "soccer", "2026-07-10")
        assert result == {"home_score": 1, "away_score": 0, "completed": True}

    def test_not_completed_returns_none(self, monkeypatch):
        body = _grounded_candidate('{"completed":false}')
        monkeypatch.setattr(
            http_utils.requests, "post",
            lambda url, json, timeout: _FakeResponse(200, body),
        )
        result = settlement.fetch_match_result("Team A vs Team B", "soccer", "2026-07-10")
        assert result is None

    def test_truncated_response_logs_diagnostic_and_returns_none(self, monkeypatch, caplog):
        """Reproduces the old maxOutputTokens=80 failure mode: the model
        spends its whole token budget and gets cut off before the closing
        '}' — finishReason=MAX_TOKENS, partial/empty text. Must not raise,
        must return None, and must log enough to diagnose it (this is what
        was previously silent — a bare `return None` indistinguishable from
        a genuine 'not found')."""
        body = _grounded_candidate('{"completed":true,"home_score":6,"awa', finish_reason="MAX_TOKENS")
        monkeypatch.setattr(
            http_utils.requests, "post",
            lambda url, json, timeout: _FakeResponse(200, body),
        )
        with caplog.at_level("WARNING"):
            result = settlement.fetch_match_result("Boston Red Sox vs New York Mets", "baseball", "2026-07-10")
        assert result is None
        assert any("finishReason" in r.message for r in caplog.records)

    def test_empty_parts_returns_none_without_crashing(self, monkeypatch):
        body = {"candidates": [{"content": {"parts": []}, "finishReason": "MAX_TOKENS"}]}
        monkeypatch.setattr(
            http_utils.requests, "post",
            lambda url, json, timeout: _FakeResponse(200, body),
        )
        result = settlement.fetch_match_result("Team A vs Team B", "soccer", "2026-07-10")
        assert result is None

    def test_non_200_returns_none(self, monkeypatch):
        monkeypatch.setattr(
            http_utils.requests, "post",
            lambda url, json, timeout: _FakeResponse(500, text="server error"),
        )
        result = settlement.fetch_match_result("Team A vs Team B", "soccer", "2026-07-10")
        assert result is None

    def test_uses_maxoutputtokens_at_least_500(self, monkeypatch):
        """Regression guard for the 80-token bug specifically — asserts the
        actual payload sent, not just behavior, so a future edit can't
        silently shrink the budget back down without failing this test."""
        captured = {}

        def fake_post(url, json, timeout):
            captured["payload"] = json
            return _FakeResponse(200, _grounded_candidate('{"completed":false}'))

        monkeypatch.setattr(http_utils.requests, "post", fake_post)
        settlement.fetch_match_result("Team A vs Team B", "soccer", "2026-07-10")
        assert captured["payload"]["generationConfig"]["maxOutputTokens"] >= 500


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


class TestPostGeminiModelFallback:
    def test_falls_through_to_next_model_on_404(self, monkeypatch):
        calls = []

        def fake_post(url, json, timeout):
            calls.append(url)
            if "gemini-2.5-flash-lite" in url:
                return _FakeResponse(404, text='{"error":{"code":404,"message":"no longer available"}}')
            return _FakeResponse(200, {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]})

        monkeypatch.setattr(http_utils.requests, "post", fake_post)
        r = http_utils.post_gemini(
            ["gemini-2.5-flash-lite", "gemini-3.5-flash"], "fake-key", {}, timeout=10, label="test")

        assert r.status_code == 200
        assert len(calls) == 2
        assert "gemini-2.5-flash-lite" in calls[0]
        assert "gemini-3.5-flash" in calls[1]

    def test_first_model_success_never_tries_second(self, monkeypatch):
        calls = []

        def fake_post(url, json, timeout):
            calls.append(url)
            return _FakeResponse(200, {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]})

        monkeypatch.setattr(http_utils.requests, "post", fake_post)
        r = http_utils.post_gemini(
            ["gemini-2.5-flash-lite", "gemini-3.5-flash"], "fake-key", {}, timeout=10, label="test")

        assert r.status_code == 200
        assert len(calls) == 1

    def test_last_model_404_returns_the_404(self, monkeypatch):
        monkeypatch.setattr(
            http_utils.requests, "post",
            lambda url, json, timeout: _FakeResponse(404, text="still not found"),
        )
        r = http_utils.post_gemini(["only-model"], "fake-key", {}, timeout=10, label="test")
        assert r.status_code == 404
