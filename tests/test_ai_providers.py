"""
tests/test_ai_providers.py — la chaîne de repli de `core/ai_search.py` passe
désormais par `core/ai_router.py` (mission 4).

Ce qui était testé ici depuis la mission 2 — clé absente = fournisseur
ignoré, budget épuisé = fournisseur suivant, quota compté dans meta — n'a pas
disparu : c'est le ROUTEUR qui le porte, et `tests/test_ai_router.py` le
vérifie sur le registre réel. Il reste ici ce qui appartient vraiment à
ai_search : la délégation, le cache et les paliers de modèles Groq.
"""
from core import ai_router, ai_search, daily_quota


class _R:
    def __init__(self, status=200, text="ok-text"):
        self.status_code = status
        self.text = text

    def json(self):
        return {"choices": [{"message": {"content": self.text}}],
                "usage": {"total_tokens": 12}}


def _no_groq(monkeypatch):
    monkeypatch.setattr(ai_search, "_groq_keys", lambda: [])
    monkeypatch.setattr(ai_search, "_cache_get", lambda k: None)
    monkeypatch.setattr(ai_search, "_cache_put", lambda k, t: None)


def _quota(monkeypatch, spent=None):
    spent = spent or {}
    added = []
    monkeypatch.setattr(daily_quota, "spent", lambda b: spent.get(b, 0))
    monkeypatch.setattr(daily_quota, "add", lambda b, n: added.append((b, n)))
    return added


def _inert_health(monkeypatch):
    """Santé en mémoire : le routeur ne doit toucher aucune base en test."""
    store = {}
    monkeypatch.setattr(ai_router, "load_health",
                        lambda n: store.get(n, {"provider": n, "consecutive_errors": 0,
                                                "breaker_until": None, "calls_today": 0,
                                                "tokens_today": 0, "failovers": []}))
    monkeypatch.setattr(ai_router, "save_health",
                        lambda h: store.__setitem__(h["provider"], h))
    return store


class TestDelegationAuRouteur:
    def test_sans_aucune_cle_aucun_appel_nest_tente(self, monkeypatch):
        _no_groq(monkeypatch); _quota(monkeypatch); _inert_health(monkeypatch)
        for p in ai_router.REGISTRY:
            monkeypatch.delenv(p.env_key, raising=False)
        calls = []
        monkeypatch.setattr(ai_router.requests, "post",
                            lambda *a, **k: calls.append(a) or _R())
        assert ai_search.ai_complete("q") is None
        assert calls == []
        assert ai_search.providers_available() == []

    def test_providers_available_lit_le_registre_du_routeur(self, monkeypatch):
        for p in ai_router.REGISTRY:
            monkeypatch.delenv(p.env_key, raising=False)
        monkeypatch.setenv("OPENROUTER_API_KEY", "o")
        monkeypatch.setenv("GROQ_API_KEY", "g")
        # Groq est exclu : `providers_available` désigne le REPLI derrière lui.
        assert ai_search.providers_available() == ["openrouter"]

    def test_le_repli_sert_la_reponse_et_compte_le_quota(self, monkeypatch):
        _no_groq(monkeypatch); added = _quota(monkeypatch); _inert_health(monkeypatch)
        for p in ai_router.REGISTRY:
            monkeypatch.delenv(p.env_key, raising=False)
        monkeypatch.setenv("OPENROUTER_API_KEY", "o")
        monkeypatch.setattr(ai_router, "fetch_catalog", lambda p, timeout=None: set())
        urls = []

        def post(url, json=None, headers=None, timeout=None):
            urls.append(url); return _R(text="depuis-openrouter")
        monkeypatch.setattr(ai_router.requests, "post", post)
        assert ai_search.ai_complete("q") == "depuis-openrouter"
        assert urls == ["https://openrouter.ai/api/v1/chat/completions"]
        assert added == [("ai_openrouter", 1)]

    def test_le_routeur_en_panne_ne_remonte_jamais_dexception(self, monkeypatch):
        _no_groq(monkeypatch); _quota(monkeypatch)
        def boom(*a, **k):
            raise RuntimeError("routeur casse")
        monkeypatch.setattr(ai_router, "route", boom)
        assert ai_search.ai_complete("q") is None

    def test_github_models_a_disparu_du_code(self):
        """HTTP 410, corps nommant le retrait : le seul mort prouvé."""
        noms = {p.name for p in ai_router.REGISTRY}
        assert "github" not in noms

    def test_le_modele_openrouter_mort_nest_plus_une_preference(self):
        """`meta-llama/llama-3.3-70b-instruct:free` a disparu du catalogue
        :free d'OpenRouter (vérifié live le 2026-08-22) ; seule la variante
        PAYANTE subsiste. Le garder en tête de liste rendait le repli mort."""
        orp = ai_router.by_name("openrouter")
        assert "meta-llama/llama-3.3-70b-instruct:free" not in orp.models


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
