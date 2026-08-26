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
        monkeypatch.delenv("GROQ_API_KEY_2", raising=False)
        monkeypatch.delenv("GROQ_API_KEY_3", raising=False)
        # Reset the per-key/per-model daily-dead map between tests.
        monkeypatch.setattr(ai_search, "_groq_dead_models", {})

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
        monkeypatch.delenv("GROQ_API_KEY_2", raising=False)
        monkeypatch.delenv("GROQ_API_KEY_3", raising=False)
        monkeypatch.setenv("TAVILY_API_KEY", "tvly-fake")
        monkeypatch.setattr(ai_search, "_groq_dead_models", {})
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
        """Quand TOUS les modèles ont pris un 429 per-day, plus aucun appel."""
        calls = []

        def fake_post(url, json=None, headers=None, timeout=None):
            calls.append(url)
            if "tavily.com" in url:
                return _FakeResponse(200, {"results": [
                    {"title": "T", "url": "u", "content": "s"}]})
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

    def test_le_TPD_de_la_tete_de_liste_ne_tue_pas_le_modele_suivant(self, monkeypatch):
        """Régression 2026-07-22 — le TPD est PAR MODÈLE.

        groq/compound-mini consomme le quota du modèle de génération en tête
        du registre. Quand ce pool est vide, le SUIVANT a encore le sien : le
        settlement doit continuer à tourner, sinon ai_learning_ledger ne
        reçoit plus rien et /performance reste figé toute la journée.

        Noms DÉRIVÉS du registre (2026-08-26) : ce test codait en dur
        `llama-3.3-70b-versatile` / `llama-3.1-8b-instant`, disparus du
        catalogue Groq — il vérifiait donc une mécanique sur des modèles que
        le pipeline n'appelle plus.
        """
        from core import ai_router
        tete, suivant = ai_router.by_name("groq").models[:2]

        models_called = []
        tpd_tete = ('{"error":{"message":"Rate limit reached for model '
                    '`' + tete + '` ... tokens per day (TPD)"}}')

        def fake_post(url, json=None, headers=None, timeout=None):
            if "tavily.com" in url:
                return _FakeResponse(200, {"results": [
                    {"title": "T", "url": "u", "content": "Final score 3-1"}]})
            model = json["model"]
            models_called.append(model)
            if model in ("groq/compound-mini", tete):
                return _FakeResponse(429, text=tpd_tete)
            return _FakeResponse(200, _groq_body('{"completed":true,"home_score":3,"away_score":1}'))

        monkeypatch.setattr(ai_search.requests, "post", fake_post)
        text = ai_search.ai_search_complete("prompt", ["query"], label="test")

        assert text == '{"completed":true,"home_score":3,"away_score":1}'
        assert suivant in models_called
        assert not ai_search.ai_dead()          # un modèle répond encore
        # compound-mini ET la tête de liste, nommée dans le corps d'erreur,
        # sont morts : un appel suivant ne doit plus les retenter.
        models_called.clear()
        ai_search.ai_search_complete("prompt2", ["query2"], label="test")
        assert models_called == [suivant]

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

    def test_un_432_condamne_tavily_pour_le_run(self, monkeypatch):
        """Régression 2026-08-26 — Tavily rendait 432 « plan usage limit » à
        CHAQUE requête : 11 aller-retours par scan, 25+ par audit, tous
        certains d'échouer, faute d'une mémoire du refus.

        Deux propriétés, et la seconde est une question de SÛRETÉ, pas de
        latence : `search_exhausted()` est ce que core/audit_engine.py teste
        avant d'écrire un état TERMINAL. Tant qu'il ne disait pas la vérité
        immédiatement, il fallait brûler les 25 crédits du budget de run pour
        que le settlement comprenne enfin qu'il n'avait pas pu CHERCHER — et
        non que l'information n'existait pas.
        """
        monkeypatch.setattr(ai_search, "_tavily_plan_dead", False)
        monkeypatch.setattr(ai_search, "_tavily_used", 0)
        monkeypatch.setattr(ai_search, "_TAVILY_RUN_BUDGET", 25)
        calls = []

        def fake_post(url, json=None, headers=None, timeout=None):
            calls.append(url)
            return _FakeResponse(432, text='{"detail":{"error":"exceeds your plan"}}')

        monkeypatch.setattr(ai_search.requests, "post", fake_post)
        assert ai_search.tavily_search("q1") == []
        assert ai_search.search_exhausted(), "le settlement doit le savoir TOUT DE SUITE"
        for i in range(5):
            assert ai_search.tavily_search(f"q{i}") == []
        assert len([c for c in calls if "tavily" in c]) == 1, \
            f"un seul aller-retour attendu, {len(calls)} effectués"
        # Remise à zéro explicite : l'état vit au niveau du MODULE et
        # fuiterait sur les tests suivants.
        monkeypatch.setattr(ai_search, "_tavily_plan_dead", False)

    def test_un_429_minute_groq_ne_dort_plus_dans_le_moteur(self, monkeypatch):
        """Régression 2026-08-26 (run Guerrilla 32990495899) — une 4e org Groq,
        neuve, répondait à la limite par MINUTE ; l'ancien code dormait 20 s
        puis 40 s à chaque 429-minute, pour chaque recherche Oracle, jusqu'au
        timeout global de 540 s : exit 1, ZÉRO signal persisté. Une clé
        vivante mais bridée faisait pire qu'une clé morte.

        Trois propriétés : (1) aucun sleep ; (2) la clé suivante est essayée
        tout de suite ; (3) un appel ultérieur, pendant le cooldown, ne
        retente pas la clé bridée — mais ne la considère pas morte pour
        autant (ai_dead reste faux : un débit par minute se repasse)."""
        monkeypatch.setenv("GROQ_API_KEY", "k1")
        monkeypatch.setenv("GROQ_API_KEY_2", "k2")
        monkeypatch.setattr(ai_search, "_groq_dead_models", {})
        monkeypatch.setattr(ai_search, "_groq_cooldown_until", {})
        monkeypatch.setattr(ai_search.time, "sleep",
                            lambda s: (_ for _ in ()).throw(AssertionError(f"sleep({s}) dans le moteur")))
        keys_seen = []

        def fake_post(url, json=None, headers=None, timeout=None):
            keys_seen.append(headers["Authorization"][-2:])
            if headers["Authorization"].endswith("k1"):
                return _FakeResponse(429, text='{"error":{"message":"Rate limit reached ... '
                                                'Please try again in 27.5s"}}')
            return _FakeResponse(200, _groq_body("OK"))

        monkeypatch.setattr(ai_search.requests, "post", fake_post)
        assert ai_search._groq_post("m", [], 16, 0.0, 5, "t") == "OK"
        assert keys_seen == ["k1", "k2"], keys_seen           # (1) et (2)
        keys_seen.clear()
        assert ai_search._groq_post("m", [], 16, 0.0, 5, "t") == "OK"
        assert keys_seen == ["k2"], "la clé en cooldown a été retentée"   # (3)
        assert not ai_search.ai_dead()
        assert ai_search._retry_after_s(_FakeResponse(429, text="try again in 1m2.5s")) == 62.5

    def test_un_429_ne_condamne_pas_tavily(self, monkeypatch):
        """Un 429 est un débit PAR MINUTE : il se repasse. Le verrouiller
        ferait perdre le reste du run pour une limite de quelques secondes."""
        monkeypatch.setattr(ai_search, "_tavily_plan_dead", False)
        monkeypatch.setattr(ai_search, "_tavily_used", 0)
        monkeypatch.setattr(ai_search.requests, "post",
                            lambda *a, **k: _FakeResponse(429, text="slow down"))
        assert ai_search.tavily_search("q1") == []
        assert not ai_search._tavily_plan_dead


class TestGroqKeyRotation:
    """core/ai_search.py : bascule sur GROQ_API_KEY_2 quand le quota
    JOURNALIER de la première clé est épuisé (2026-08-02).

    Le TPD Groq est compté par ORGANISATION : ces tests garantissent qu'une
    2e clé est réellement essayée, sans quoi MMA/tennis de table/volley —
    qui n'existent que via la recherche web — disparaissent dès ~10h UTC.
    """

    _TPD = ('{"error":{"message":"Rate limit reached for model '
            '`llama-3.3-70b-versatile` ... tokens per day (TPD)"}}')

    @pytest.fixture(autouse=True)
    def _keys(self, monkeypatch):
        monkeypatch.setenv("GROQ_API_KEY", "gsk-key-one")
        monkeypatch.setenv("GROQ_API_KEY_2", "gsk-key-two")
        monkeypatch.delenv("GROQ_API_KEY_3", raising=False)
        monkeypatch.delenv("TAVILY_API_KEY", raising=False)
        monkeypatch.setattr(ai_search, "_groq_dead_models", {})
        monkeypatch.setattr(ai_search, "_tavily_used", 0)

    @staticmethod
    def _key_of(headers):
        return headers["Authorization"].removeprefix("Bearer ")

    def test_daily_quota_on_key_one_falls_through_to_key_two(self, monkeypatch):
        used = []

        def fake_post(url, json=None, headers=None, timeout=None):
            key = self._key_of(headers)
            used.append(key)
            if key == "gsk-key-one":
                return _FakeResponse(429, text=self._TPD)
            return _FakeResponse(200, _groq_body('{"ok":true}'))

        monkeypatch.setattr(ai_search.requests, "post", fake_post)
        assert ai_search.ai_search_complete("p", ["q"], label="t") == '{"ok":true}'
        assert used == ["gsk-key-one", "gsk-key-two"]
        assert not ai_search.ai_dead()   # la clé 2 sert encore

    def test_dead_key_is_not_retried_on_later_calls(self, monkeypatch):
        used = []

        def fake_post(url, json=None, headers=None, timeout=None):
            key = self._key_of(headers)
            used.append(key)
            if key == "gsk-key-one":
                return _FakeResponse(429, text=self._TPD)
            return _FakeResponse(200, _groq_body('{"ok":true}'))

        monkeypatch.setattr(ai_search.requests, "post", fake_post)
        ai_search.ai_search_complete("p", ["q"], label="t")
        used.clear()
        ai_search.ai_search_complete("p2", ["q2"], label="t")
        assert used == ["gsk-key-two"]   # plus aucun appel gaspillé sur la clé 1

    def test_ai_dead_only_when_every_key_is_exhausted(self, monkeypatch):
        """Tavily doit répondre, sinon l'étage d'extraction n'est jamais
        atteint et llama-3.1-8b-instant garde son quota intact — ai_dead()
        resterait False à raison."""
        monkeypatch.setenv("TAVILY_API_KEY", "tvly-fake")
        used = []

        def fake_post(url, json=None, headers=None, timeout=None):
            if "tavily.com" in url:
                return _FakeResponse(200, {"results": [
                    {"title": "T", "url": "u", "content": "s"}]})
            used.append(self._key_of(headers))
            return _FakeResponse(429, text=self._TPD)

        monkeypatch.setattr(ai_search.requests, "post", fake_post)
        assert ai_search.ai_search_complete("p", ["q"], label="t") is None
        assert set(used) == {"gsk-key-one", "gsk-key-two"}
        assert ai_search.ai_dead()

    def test_same_value_in_both_secrets_is_deduplicated(self, monkeypatch):
        """Coller la même clé dans les deux secrets ne rachète aucun quota —
        la retenter donnerait juste un 429 de plus et un log mensonger."""
        monkeypatch.setenv("GROQ_API_KEY_2", "gsk-key-one")
        monkeypatch.setenv("TAVILY_API_KEY", "tvly-fake")
        used = []

        def fake_post(url, json=None, headers=None, timeout=None):
            if "tavily.com" in url:
                return _FakeResponse(200, {"results": [
                    {"title": "T", "url": "u", "content": "s"}]})
            used.append(self._key_of(headers))
            return _FakeResponse(429, text=self._TPD)

        monkeypatch.setattr(ai_search.requests, "post", fake_post)
        ai_search.ai_search_complete("p", ["q"], label="t")
        assert set(used) == {"gsk-key-one"}
        assert ai_search.ai_dead()

    def test_413_does_not_burn_the_second_key(self, monkeypatch):
        """Le TPM est le même partout : rejouer une requête trop grosse sur la
        clé 2 la ferait échouer à l'identique. L'étage Tavily est le vrai plan B."""
        used = []

        def fake_post(url, json=None, headers=None, timeout=None):
            used.append(self._key_of(headers))
            return _FakeResponse(413, text="Request too large")

        monkeypatch.setattr(ai_search.requests, "post", fake_post)
        ai_search.ai_search_complete("p", ["q"], label="t")
        assert used == ["gsk-key-one"]
