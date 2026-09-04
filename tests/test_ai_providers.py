"""
tests/test_ai_providers.py — la façade `core/ai_search.py` délègue TOUT au
routeur (`core/ai_router.py`) depuis la suppression de Groq/Tavily (2026-09-02).

Ce qui était testé ici depuis la mission 2 — clé absente = fournisseur
ignoré, budget épuisé = fournisseur suivant, quota compté dans meta — n'a pas
disparu : c'est le ROUTEUR qui le porte, et `tests/test_ai_router.py` le
vérifie sur le registre réel. Il reste ici ce qui appartient vraiment à
ai_search : la délégation, le cache, et l'absence de tout client direct.
"""
from core import ai_router, ai_search, daily_quota


class _R:
    def __init__(self, status=200, text="ok-text"):
        self.status_code = status
        self.text = text

    def json(self):
        return {"choices": [{"message": {"content": self.text}}],
                "usage": {"total_tokens": 12}}


def _sans_cache(monkeypatch):
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
        _sans_cache(monkeypatch); _quota(monkeypatch); _inert_health(monkeypatch)
        for p in ai_router.REGISTRY:
            monkeypatch.delenv(p.env_key, raising=False)
        calls = []
        monkeypatch.setattr(ai_router.requests, "post",
                            lambda *a, **k: calls.append(a) or _R())
        assert ai_search.ai_complete("q") is None
        assert calls == []
        assert ai_search.providers_available() == []
        assert ai_search.ai_available() is False

    def test_providers_available_lit_le_registre_du_routeur(self, monkeypatch):
        for p in ai_router.REGISTRY:
            monkeypatch.delenv(p.env_key, raising=False)
        monkeypatch.setenv("OPENROUTER_API_KEY", "o")
        assert ai_search.providers_available() == ["openrouter"]
        assert ai_search.ai_available() is True

    def test_le_routeur_sert_la_reponse_et_compte_le_quota(self, monkeypatch):
        _sans_cache(monkeypatch); added = _quota(monkeypatch); _inert_health(monkeypatch)
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
        _sans_cache(monkeypatch); _quota(monkeypatch)
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
        monkeypatch.setattr(ai_search, "_fallback_post",
                            lambda *a, **k: calls.append(1) or "x")
        assert ai_search.ai_complete("same prompt") == "cached!"
        assert calls == []

    def test_cache_key_is_normalised(self):
        a = ai_search._cache_key("  Find   the SCORE ", ["q1", "Q2 "])
        b = ai_search._cache_key("find the score", ["q2", "q1"])
        assert a == b and a.startswith("ai_cache_")

    def test_le_palier_choisit_la_lane_du_routeur(self, monkeypatch):
        """`tier` ne réordonne plus des modèles : il choisit la LANE du
        routeur — "light" → filter, "heavy" → analyze."""
        _sans_cache(monkeypatch)
        lanes = []

        def fake(messages, max_tokens, temperature, timeout, label, lane="analyze"):
            lanes.append(lane); return "t"
        monkeypatch.setattr(ai_search, "_fallback_post", fake)
        ai_search.ai_complete("q", tier="light")
        ai_search.ai_complete("q", tier="heavy")
        ai_search.ai_complete("q", lane="filter")
        assert lanes == ["filter", "analyze", "filter"]
