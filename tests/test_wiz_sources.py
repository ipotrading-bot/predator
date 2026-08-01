"""
tests/test_wiz_sources.py — la cascade de sources de Wiz.

Mesuré le 2026-08-01 : 79 des 93 analyses de `wiz_analysis` sont INDISPONIBLE
(85%), parce que Wiz n'avait qu'une source — le connecteur `web_search` de
Mistral, dont le quota (au niveau du COMPTE) est épuisé depuis le 2026-07-23.

Épinglé ici :
  - la fraîcheur n'est pas optionnelle : sans `when:Nd`, le flux remonte des
    articles de l'an dernier, et un « titulaire absent » périmé est une fausse
    information, pas une source faible ;
  - la source gratuite passe AVANT Tavily, et Tavily n'est touché qu'au-dessus
    de la réserve du moteur (invariant : Wiz ne peut pas affamer un
    settlement) ;
  - aucune source ne lève jamais : une panne vaut « rien trouvé », sinon Wiz
    ne peut pas enchaîner sur la suivante ;
  - sans aucune source, la cascade rend (None, [], None) → INDISPONIBLE, et
    surtout PAS un verdict non sourcé.
"""
import pytest

import core.wiz_sources as ws


_RSS = b"""<?xml version="1.0"?>
<rss version="2.0" xmlns:news="http://news.google.com/">
  <channel>
    <item>
      <title>Star striker ruled out of derby</title>
      <link>https://news.google.com/rss/articles/AAA</link>
      <description>&lt;a href="x"&gt;Club confirms&lt;/a&gt; the forward is out</description>
      <pubDate>Fri, 31 Jul 2026 17:30:00 GMT</pubDate>
      <source url="https://onefootball.com">OneFootball</source>
    </item>
    <item>
      <title>Line-ups confirmed</title>
      <link>https://news.google.com/rss/articles/BBB</link>
      <description>Both sides name unchanged XI</description>
      <pubDate>Sat, 01 Aug 2026 09:00:00 GMT</pubDate>
    </item>
    <item>
      <title>Sans lien</title>
      <link></link>
    </item>
  </channel>
</rss>"""


class _Resp:
    def __init__(self, content, status=200):
        self.content = content
        self.status_code = status


class TestGoogleNews:
    def test_parses_articles(self, monkeypatch):
        monkeypatch.setattr(ws.requests, "get", lambda *a, **k: _Resp(_RSS))
        out = ws.google_news("Flamengo vs Palmeiras")
        assert [x["title"] for x in out] == ["Star striker ruled out of derby",
                                             "Line-ups confirmed"]
        assert out[0]["url"] == "https://news.google.com/rss/articles/AAA"
        assert out[0]["content"] == "Club confirms the forward is out"   # HTML nettoyé
        assert out[0]["source"] == "OneFootball"

    def test_freshness_operator_is_always_sent(self, monkeypatch):
        seen = {}

        def fake_get(url, **_k):
            seen["url"] = url
            return _Resp(_RSS)

        monkeypatch.setattr(ws.requests, "get", fake_get)
        ws.google_news("Yankees vs Red Sox", within_days=2)
        assert "when%3A2d" in seen["url"]

    @pytest.mark.parametrize("boom", [
        lambda *a, **k: _Resp(b"pas du xml"),
        lambda *a, **k: _Resp(b"", status=503),
        lambda *a, **k: (_ for _ in ()).throw(OSError("réseau")),
    ])
    def test_failure_means_no_result_never_an_exception(self, monkeypatch, boom):
        monkeypatch.setattr(ws.requests, "get", boom)
        assert ws.google_news("peu importe") == []


class TestCascadeOrder:
    def test_free_source_first_tavily_untouched(self, monkeypatch):
        called = {"tavily": 0}
        monkeypatch.setattr(ws, "google_news",
                            lambda q, *a, **k: [{"url": f"https://free/{q}", "title": q}])
        monkeypatch.setattr(ws, "_tavily",
                            lambda q, *a, **k: called.__setitem__("tavily", called["tavily"] + 1) or [])
        out = ws.gather(["q1", "q2"])
        assert len(out) == 2 and called["tavily"] == 0

    def test_tavily_takes_over_when_free_source_is_empty(self, monkeypatch):
        monkeypatch.setattr(ws, "google_news", lambda *a, **k: [])
        monkeypatch.setattr(ws, "_tavily",
                            lambda q, *a, **k: [{"url": "https://tav/1", "title": "t"}])
        assert len(ws.gather(["q1"])) == 1

    def test_duplicate_urls_are_collapsed(self, monkeypatch):
        monkeypatch.setattr(ws, "google_news",
                            lambda *a, **k: [{"url": "https://same", "title": "x"}])
        monkeypatch.setattr(ws, "_tavily", lambda *a, **k: [])
        assert len(ws.gather(["q1", "q2"])) == 1

    def test_a_broken_source_does_not_break_the_gather(self, monkeypatch):
        monkeypatch.setattr(ws, "google_news",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        monkeypatch.setattr(ws, "_tavily",
                            lambda *a, **k: [{"url": "https://tav/1", "title": "t"}])
        assert len(ws.gather(["q1"])) == 1


class TestTavilyReserve:
    def test_engine_credits_are_never_eaten_by_wiz(self, monkeypatch):
        from core import ai_search
        monkeypatch.setattr(ai_search, "search_credits_left", lambda: ws.WIZ_TAVILY_RESERVE)
        monkeypatch.setattr(ai_search, "tavily_search",
                            lambda *a, **k: pytest.fail("Wiz a consommé la réserve du moteur"))
        assert ws._tavily("q") == []

    def test_tavily_used_above_the_reserve(self, monkeypatch):
        from core import ai_search
        monkeypatch.setattr(ai_search, "search_credits_left", lambda: ws.WIZ_TAVILY_RESERVE + 5)
        monkeypatch.setattr(ai_search, "tavily_search",
                            lambda *a, **k: [{"url": "https://tav", "title": "t", "content": ""}])
        assert len(ws._tavily("q")) == 1


class TestSearchFn:
    ctx = {"match": "A vs B", "sport": "soccer", "market_keys": ["h2h"], "kickoff": ""}

    def test_nominal_connector_wins_when_it_answers(self, monkeypatch):
        from core import wiz_ai
        monkeypatch.setattr(wiz_ai, "search_quota_dead", lambda: False)
        monkeypatch.setattr(wiz_ai, "mistral_search",
                            lambda p, label="WIZ": ("{}", [{"url": "u"}], "mistral-x"))
        monkeypatch.setattr(ws, "gather", lambda _q: pytest.fail("cascade inutile"))
        text, sources, model = ws.make_search_fn(self.ctx)("prompt")
        assert (text, model) == ("{}", "mistral-x")

    def test_falls_back_to_external_sources_then_chat(self, monkeypatch):
        from core import wiz_ai
        monkeypatch.setattr(wiz_ai, "search_quota_dead", lambda: True)
        monkeypatch.setattr(ws, "gather",
                            lambda _q: [{"url": "https://n/1", "title": "t", "content": "c"}])
        monkeypatch.setattr(wiz_ai, "mistral_complete",
                            lambda p, label="WIZ": ('{"verdict":"NEUTRE"}', "mistral-small"))
        text, sources, model = ws.make_search_fn(self.ctx)("prompt")
        assert model == "mistral-small" and len(sources) == 1
        assert "verdict" in text

    def test_groq_is_the_last_resort(self, monkeypatch):
        from core import ai_search, wiz_ai
        monkeypatch.setattr(wiz_ai, "search_quota_dead", lambda: True)
        monkeypatch.setattr(ws, "gather", lambda _q: [{"url": "https://n/1", "title": "t"}])
        monkeypatch.setattr(wiz_ai, "mistral_complete", lambda p, label="WIZ": (None, None))
        monkeypatch.setattr(ai_search, "ai_complete", lambda p, label="AI": '{"verdict":"NEUTRE"}')
        _text, _sources, model = ws.make_search_fn(self.ctx)("prompt")
        assert model == "groq"

    def test_no_source_yields_unavailable_not_an_unsourced_verdict(self, monkeypatch):
        from core import wiz_ai
        monkeypatch.setattr(wiz_ai, "search_quota_dead", lambda: True)
        monkeypatch.setattr(ws, "gather", lambda _q: [])
        monkeypatch.setattr(wiz_ai, "mistral_complete",
                            lambda *a, **k: pytest.fail("aucun raisonnement sans source"))
        assert ws.make_search_fn(self.ctx)("prompt") == (None, [], None)


def test_format_results_repeats_the_url_verbatim():
    # R4 : validate() rejette tout argument dont l'URL n'est pas dans le set
    # fourni — le modèle doit pouvoir la recopier, pas la deviner.
    block = ws.format_results([{"title": "T", "url": "https://ex.com/a",
                                "content": "c", "source": "ESPN", "published": "hier"}])
    assert "https://ex.com/a" in block and "ESPN · hier" in block


class TestCascadeAvailability:
    """`cascade_available()` remplace `wiz_ai.wiz_available()` partout où l'on
    décidait d'arrêter un run. C'est le raccourci « connecteur mort = fin de
    partie » qui a produit 79 INDISPONIBLE sur 93."""

    def test_groq_alone_keeps_wiz_alive(self, monkeypatch):
        from core import ai_search, wiz_ai
        monkeypatch.setattr(wiz_ai, "wiz_available", lambda: False)
        monkeypatch.setattr(ai_search, "ai_available", lambda: True)
        assert ws.cascade_available() is True

    def test_dead_mistral_connector_is_not_a_dead_cascade(self, monkeypatch):
        from core import ai_search, wiz_ai
        monkeypatch.setattr(wiz_ai, "wiz_available", lambda: True)
        monkeypatch.setattr(wiz_ai, "wiz_dead", lambda: True)     # modèles morts
        monkeypatch.setattr(ai_search, "ai_available", lambda: True)
        assert ws.cascade_available() is True

    def test_no_reasoning_provider_at_all(self, monkeypatch):
        from core import ai_search, wiz_ai
        monkeypatch.setattr(wiz_ai, "wiz_available", lambda: False)
        monkeypatch.setattr(ai_search, "ai_available", lambda: False)
        assert ws.cascade_available() is False


def test_run_budget_is_not_conflated_with_connector_quota(monkeypatch):
    # run_wiz.py sortait de la boucle au premier 429 du connecteur parce que
    # search_exhausted() fusionnait les deux causes. run_budget_exhausted()
    # ne parle QUE de la durée du run.
    from core import wiz_ai
    monkeypatch.setattr(wiz_ai, "_search_quota_dead", True, raising=False)
    monkeypatch.setattr(wiz_ai, "_searches_used", 0, raising=False)
    assert wiz_ai.search_exhausted() is True
    assert wiz_ai.run_budget_exhausted() is False
