"""
tests/test_ai_providers.py — Mission 2, Phase 3 : capacité IA par des voies
légitimes. Un seul compte par fournisseur ; la capacité vient de la
DIVERSIFICATION (OpenRouter, Cerebras, GitHub Models en repli derrière
Groq, chacun optionnel et avec son quota journalier dans meta) et de la
RÉDUCTION de consommation (cache 30 min, palier léger/lourd).
"""
from core import ai_search, daily_quota


class _R:
    def __init__(self, status=200, text="ok-text"):
        self.status_code = status
        self.text = text

    def json(self):
        return {"choices": [{"message": {"content": self.text}}]}


def _no_groq(monkeypatch):
    monkeypatch.setattr(ai_search, "_groq_keys", lambda: [])
    monkeypatch.setattr(ai_search, "_cache_get", lambda k: None)
    monkeypatch.setattr(ai_search, "_cache_put", lambda k, t: None)
    ai_search._provider_dead.clear()


def _quota(monkeypatch, spent=None):
    spent = spent or {}
    added = []
    monkeypatch.setattr(daily_quota, "spent", lambda b: spent.get(b, 0))
    monkeypatch.setattr(daily_quota, "add", lambda b, n: added.append((b, n)))
    return added


class TestFallbackChain:
    def test_absent_key_means_provider_ignored(self, monkeypatch):
        _no_groq(monkeypatch); _quota(monkeypatch)
        for p in ai_search._FALLBACK_PROVIDERS:
            monkeypatch.delenv(p["env"], raising=False)
        calls = []
        monkeypatch.setattr(ai_search.requests, "post", lambda *a, **k: calls.append(a) or _R())
        assert ai_search.ai_complete("q") is None
        assert calls == []
        assert ai_search.providers_available() == []

    def test_first_configured_provider_serves_and_is_metered(self, monkeypatch):
        _no_groq(monkeypatch); added = _quota(monkeypatch)
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        monkeypatch.setenv("CEREBRAS_API_KEY", "c-key")
        monkeypatch.delenv("GITHUB_MODELS_TOKEN", raising=False)
        urls = []

        def post(url, json=None, headers=None, timeout=None):
            urls.append(url); return _R(text="from-cerebras")
        monkeypatch.setattr(ai_search.requests, "post", post)
        assert ai_search.ai_complete("q") == "from-cerebras"
        assert urls == ["https://api.cerebras.ai/v1/chat/completions"]
        assert added == [("ai_cerebras", 1)]

    def test_exhausted_budget_skips_to_next_provider(self, monkeypatch):
        _no_groq(monkeypatch)
        _quota(monkeypatch, spent={"ai_openrouter": 10_000})
        monkeypatch.setenv("OPENROUTER_API_KEY", "o")
        monkeypatch.setenv("CEREBRAS_API_KEY", "c")
        urls = []

        def post(url, json=None, headers=None, timeout=None):
            urls.append(url); return _R()
        monkeypatch.setattr(ai_search.requests, "post", post)
        assert ai_search.ai_complete("q") == "ok-text"
        assert urls == ["https://api.cerebras.ai/v1/chat/completions"]

    def test_429_marks_provider_dead_for_the_process(self, monkeypatch):
        _no_groq(monkeypatch); _quota(monkeypatch)
        monkeypatch.setenv("OPENROUTER_API_KEY", "o")
        monkeypatch.setenv("CEREBRAS_API_KEY", "c")
        urls = []

        def post(url, json=None, headers=None, timeout=None):
            urls.append(url)
            return _R(status=429) if "openrouter" in url else _R(text="cb")
        monkeypatch.setattr(ai_search.requests, "post", post)
        assert ai_search.ai_complete("q") == "cb"
        assert "openrouter" in ai_search._provider_dead
        urls.clear()
        ai_search.ai_complete("q2")
        assert all("openrouter" not in u for u in urls)


class TestConsumptionReduction:
    def test_cache_hit_makes_no_call_at_all(self, monkeypatch):
        monkeypatch.setattr(ai_search, "_cache_get", lambda k: "cached!")
        calls = []
        monkeypatch.setattr(ai_search, "_groq_post", lambda *a, **k: calls.append(1) or "x")
        monkeypatch.setattr(ai_search.requests, "post", lambda *a, **k: calls.append(1) or _R())
        assert ai_search.ai_complete("same prompt") == "cached!"
        assert ai_search.ai_search_complete("same prompt", ["q"]) == "cached!"
        assert calls == []

    def test_cache_key_is_normalised(self):
        a = ai_search._cache_key("  Find   the SCORE ", ["q1", "Q2 "])
        b = ai_search._cache_key("find the score", ["q2", "q1"])
        assert a == b and a.startswith("ai_cache_")

    def test_light_tier_tries_the_small_model_first(self, monkeypatch):
        monkeypatch.setattr(ai_search, "_cache_get", lambda k: None)
        monkeypatch.setattr(ai_search, "_cache_put", lambda k, t: None)
        order = []

        def groq_post(model, *a, **k):
            order.append(model); return "t"
        monkeypatch.setattr(ai_search, "_groq_post", groq_post)
        ai_search.ai_complete("q", tier="light")
        ai_search.ai_complete("q", tier="heavy")
        assert order == ["llama-3.1-8b-instant", "llama-3.3-70b-versatile"]

    def test_estimator_uses_the_light_tier(self):
        import inspect
        from core import harvester
        assert 'tier="light"' in inspect.getsource(harvester.fetch_estimated_prices)
