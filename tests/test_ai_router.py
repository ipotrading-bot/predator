"""
tests/test_ai_router.py — registre, lanes, découverte de catalogue, bascule,
disjoncteur et réserve de settlement.

LE TEST QUI COMPTE
------------------
`TestBasculeDeModele` simule la MORT d'un modèle et d'un fournisseur. C'est la
panne que ce module existe pour empêcher, et elle n'était pas hypothétique :
au moment d'écrire la mission 4, `core/ai_search.py` portait en dur
`meta-llama/llama-3.3-70b-instruct:free`, qui avait déjà disparu du catalogue
`:free` d'OpenRouter — le repli était mort, en silence, depuis on ne sait
quand.

Aucun réseau (tests/conftest.py) : `fetch_catalog` et `requests.post` sont
stubbés, la santé vit en mémoire.
"""
import pytest

from core import ai_router as R
from core import daily_quota


@pytest.fixture(autouse=True)
def _isole(monkeypatch):
    """Santé en mémoire, catalogues vides par défaut, quota inerte."""
    store = {}

    def load(name):
        return store.get(name, {"provider": name, "consecutive_errors": 0,
                                "breaker_until": None, "last_success": None,
                                "tokens_today": 0, "calls_today": 0,
                                "failovers": [], "missing_models": []})
    monkeypatch.setattr(R, "load_health", load)
    monkeypatch.setattr(R, "save_health", lambda h: store.__setitem__(h["provider"], h))
    monkeypatch.setattr(daily_quota, "spent", lambda b: 0)
    monkeypatch.setattr(daily_quota, "add", lambda b, n: None)
    R._catalog_cache.clear()
    for p in R.REGISTRY:
        monkeypatch.delenv(p.env_key, raising=False)
    yield store
    R._catalog_cache.clear()


def _R(status=200, text="ok", tokens=7):
    class Resp:
        status_code = status
        @staticmethod
        def json():
            return {"choices": [{"message": {"content": text}}],
                    "usage": {"total_tokens": tokens}}
    Resp.text = text
    return Resp


class TestRegistre:
    def test_un_fournisseur_sans_cle_est_ignore_silencieusement(self):
        """Zéro dépendance obligatoire — un déploiement qui ne configure que
        Groq doit tourner sans un seul warning."""
        assert R.active_providers() == []

    def test_seuls_les_fournisseurs_avec_cle_sont_actifs(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "o")
        assert [p.name for p in R.active_providers()] == ["openrouter"]

    def test_seul_github_models_est_reellement_mort(self):
        """GitHub Models rend HTTP 410 dont le CORPS nomme le retrait
        (« github_models_retirement_brownout ») : preuve directe.

        Cerebras, lui, a été retiré à tort au premier passage sur la foi d'un
        403 sans clé — qui ne prouve rien : c'est la signature d'un endpoint
        authentifié par clé, comme Scaleway ou Cohere qui rendent 401 dans les
        mêmes conditions. Avec une clé invalide il répond
        `401 {"code":"wrong_api_key"}`, donc il est vivant. Il est rétabli."""
        noms = {p.name for p in R.REGISTRY}
        assert "github" not in noms and "github_models" not in noms
        assert "cerebras" in noms

    def test_les_endpoints_anonymes_ne_sont_pas_enroles(self):
        """Même défaut fatal que les sources sans clé de l'incident d'août :
        filtrage par IP depuis les runners, et CGU floues."""
        noms = {p.name for p in R.REGISTRY}
        assert not ({"pollinations", "llm7"} & noms)
        assert all(p.env_key for p in R.REGISTRY)

    def test_chaque_fournisseur_declare_au_moins_une_lane(self):
        for p in R.REGISTRY:
            assert p.lanes, p.name
            assert set(p.lanes) <= set(R.LANES), p.name

    def test_un_seul_compte_par_fournisseur(self):
        """La capacité vient de la DIVERSITÉ, jamais de comptes multiples :
        aucune variable d'env ne doit être un doublon numéroté."""
        keys = [p.env_key for p in R.REGISTRY]
        assert len(keys) == len(set(keys))
        assert not any(k.rstrip("0123456789").endswith("_") and k[-1].isdigit()
                       for k in keys)


class TestConditionsDUtilisation:
    def test_les_fournisseurs_non_commerciaux_sont_marques(self):
        flagged = {p.name: p.terms_flag for p in R.REGISTRY if p.terms_flag}
        assert flagged["cohere"] == "non_commercial"
        assert flagged["zhipu"] == "non_commercial"
        assert flagged["nvidia_nim"] == "evaluation"

    def test_un_fournisseur_marque_est_exclu_de_la_production(self, monkeypatch):
        monkeypatch.setenv("COHERE_API_KEY", "c")
        monkeypatch.setattr(R, "fetch_catalog", lambda p, timeout=None: set())
        assert R.lane_providers(R.ANALYZE) == []
        # …mais reste disponible pour l'expérimentation, explicitement.
        assert [p.name for p, _m in R.lane_providers(R.ANALYZE, allow_flagged=True)] == ["cohere"]


class TestBasculeDeModele:
    """La preuve de bascule exigée par le livrable."""

    def _prov(self, models):
        return R.Provider(name="essai", base_url="https://x/v1", env_key="X_KEY",
                          models=models, lanes=(R.ANALYZE,))

    def test_le_modele_prefere_vivant_est_retenu_sans_bascule(self):
        p = self._prov(("bon", "secours"))
        assert R.resolve_model(p, {"bon", "secours"}) == ("bon", False)

    def test_la_mort_du_modele_prefere_declenche_une_bascule(self):
        """LE cas réel : `meta-llama/llama-3.3-70b-instruct:free` a disparu du
        catalogue :free d'OpenRouter. Le routeur doit prendre le suivant et
        le DIRE, pas échouer en silence."""
        p = self._prov(("mort", "secours"))
        assert R.resolve_model(p, {"secours", "autre"}) == ("secours", True)

    def test_si_aucune_preference_ne_survit_le_fournisseur_est_ecarte(self):
        p = self._prov(("mort-a", "mort-b"))
        model, switched = R.resolve_model(p, {"rien-a-voir"})
        assert model is None and switched is True

    def test_un_catalogue_illisible_ne_debranche_pas_le_fournisseur(self):
        """Un `/models` momentanément muet ne prouve pas qu'un fournisseur est
        mort. Ensemble vide = « je ne sais pas », pas « aucun modèle »."""
        p = self._prov(("prefere", "secours"))
        assert R.resolve_model(p, set()) == ("prefere", False)

    def test_le_catalogue_accepte_les_trois_formes_de_reponse(self, monkeypatch):
        for body in ({"data": [{"id": "m1"}]},
                     {"models": [{"name": "m1"}]},
                     {"data": ["m1"]}):
            R._catalog_cache.clear()
            monkeypatch.setattr(R.requests, "get", lambda *a, **k: type(
                "X", (), {"status_code": 200, "json": staticmethod(lambda: body)})())
            p = R.Provider(name=f"f{id(body)}", base_url="https://x/v1",
                           env_key="X", models=("m1",), lanes=(R.ANALYZE,))
            assert "m1" in R.fetch_catalog(p)


class TestAlerteDeLane:
    def test_une_lane_sous_deux_fournisseurs_sains_alerte(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "o")
        monkeypatch.setattr(R, "fetch_catalog", lambda p, timeout=None: set())
        envoyes = []
        rapport = R.refresh_catalogues(alert=envoyes.append)
        assert rapport["alerts"]
        assert envoyes and "santé IA" in envoyes[0]

    def test_deux_fournisseurs_sains_ne_declenchent_rien(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "o")
        monkeypatch.setenv("GROQ_API_KEY", "g")
        monkeypatch.setenv("SAMBANOVA_API_KEY", "s")
        monkeypatch.setenv("OVH_AI_API_KEY", "v")
        monkeypatch.setenv("MODELSCOPE_API_KEY", "m")
        monkeypatch.setenv("SCALEWAY_API_KEY", "sc")
        monkeypatch.setenv("OLLAMA_API_KEY", "ol")
        monkeypatch.setattr(R, "fetch_catalog", lambda p, timeout=None: set())
        rapport = R.refresh_catalogues(alert=None)
        for lane in (R.FILTER, R.ANALYZE, R.SETTLEMENT):
            assert len(rapport["lanes"][lane]) >= R.LANE_MIN_HEALTHY, lane

    def test_la_lane_wiz_nalerte_jamais(self, monkeypatch):
        """Wiz est mono-fournisseur PAR CONSTRUCTION (domaine de panne isolé,
        core/wiz_ai.py). L'alerter à chaque run serait du bruit permanent."""
        monkeypatch.setenv("OPENROUTER_API_KEY", "o")
        monkeypatch.setattr(R, "fetch_catalog", lambda p, timeout=None: set())
        rapport = R.refresh_catalogues(alert=None)
        assert not any("wiz" in a for a in rapport["alerts"])

    def test_aucun_fournisseur_configure_nalerte_pas(self, monkeypatch):
        """Zéro fournisseur n'est pas une dégradation : c'est un choix de
        déploiement (mode REPRICE, sandbox). Alerter enverrait un message par
        lane à CHAQUE run — le bruit qui fait qu'on n'ouvre plus les alertes,
        donc qu'on rate la vraie. Régression de test_reprice_mode.py."""
        envoyes = []
        rapport = R.refresh_catalogues(alert=envoyes.append)
        assert rapport["alerts"] == [] and envoyes == []
        assert set(rapport["lanes"]) == set(R.LANES)

    def test_une_alerte_qui_echoue_ne_casse_pas_le_run(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "o")
        monkeypatch.setattr(R, "fetch_catalog", lambda p, timeout=None: set())

        def boom(_):
            raise RuntimeError("telegram down")
        R.refresh_catalogues(alert=boom)          # ne lève pas


class TestDisjoncteur:
    def test_trois_echecs_consecutifs_ouvrent_le_disjoncteur(self):
        h = {"provider": "x", "consecutive_errors": 0}
        for _ in range(R.BREAKER_THRESHOLD - 1):
            h = R.record_failure(h)
            assert not R.breaker_open(h)
        h = R.record_failure(h)
        assert R.breaker_open(h)

    def test_un_succes_referme_le_disjoncteur(self):
        h = {"provider": "x", "consecutive_errors": 5,
             "breaker_until": "2999-01-01T00:00:00+00:00"}
        assert R.breaker_open(h)
        h = R.record_success(h, tokens=10)
        assert not R.breaker_open(h)
        assert h["consecutive_errors"] == 0 and h["tokens_today"] == 10

    def test_un_fournisseur_au_repos_est_ecarte_de_la_lane(self, monkeypatch, _isole):
        monkeypatch.setenv("OPENROUTER_API_KEY", "o")
        monkeypatch.setattr(R, "fetch_catalog", lambda p, timeout=None: set())
        assert [p.name for p, _m in R.lane_providers(R.ANALYZE)] == ["openrouter"]
        _isole["openrouter"] = {"provider": "openrouter", "consecutive_errors": 3,
                                "breaker_until": "2999-01-01T00:00:00+00:00"}
        assert R.lane_providers(R.ANALYZE) == []

    def test_une_reponse_vide_compte_comme_un_echec(self, monkeypatch):
        """Elle consomme le quota sans rien produire : c'est une panne, pas un
        succès silencieux."""
        monkeypatch.setenv("OPENROUTER_API_KEY", "o")
        monkeypatch.setattr(R.requests, "post", lambda *a, **k: _R(text="   "))
        p = R.by_name("openrouter")
        assert R.call_provider(p, "m", [], 10, 0.0, 5, "t") is None
        assert R.load_health("openrouter")["consecutive_errors"] == 1

    def test_une_reponse_invalide_compte_comme_un_echec_et_passe_au_suivant(self, monkeypatch):
        """JSON invalide = fournisseur en panne du point de vue du pipeline."""
        monkeypatch.setenv("OPENROUTER_API_KEY", "o")
        monkeypatch.setenv("SAMBANOVA_API_KEY", "s")
        monkeypatch.setattr(R, "fetch_catalog", lambda p, timeout=None: set())
        vus = []

        def post(url, json=None, headers=None, timeout=None):
            vus.append(url)
            return _R(text="pas du json" if "openrouter" in url else '{"ok":1}')
        monkeypatch.setattr(R.requests, "post", post)
        import json as _json

        def valide(t):
            _json.loads(t)
            return True
        text, prov = R.route([], R.ANALYZE, allow_flagged=True, validator=valide)
        assert text == '{"ok":1}' and prov == "sambanova"
        assert len(vus) == 2


class TestPasDeDoubleDepense:
    def test_un_prompt_valide_nest_jamais_rejoue_ailleurs(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "o")
        monkeypatch.setenv("SAMBANOVA_API_KEY", "s")
        monkeypatch.setattr(R, "fetch_catalog", lambda p, timeout=None: set())
        vus = []

        def post(url, json=None, headers=None, timeout=None):
            vus.append(url); return _R(text="bonne reponse")
        monkeypatch.setattr(R.requests, "post", post)
        text, prov = R.route([], R.ANALYZE)
        assert text == "bonne reponse" and prov == "openrouter"
        assert len(vus) == 1            # un seul appel, pas deux


class TestReserveSettlement:
    def test_les_autres_lanes_ne_peuvent_pas_entamer_la_reserve(self, monkeypatch):
        """Le 2026-08-02, le scan a épuisé le TPD Groq et le settlement n'a
        plus rien réglé de la journée. La réserve est gardée EN NÉGATIF : les
        autres lanes s'arrêtent avant, elles n'y ont jamais accès."""
        groq = R.by_name("groq")
        reste = groq.daily_requests - R.SETTLEMENT_RESERVE
        monkeypatch.setattr(daily_quota, "spent", lambda b: reste)
        assert R.budget_left(groq, R.FILTER) == 0
        assert R.budget_left(groq, R.SETTLEMENT) == R.SETTLEMENT_RESERVE

    def test_la_lane_settlement_garde_son_budget_quand_le_scan_a_tout_pris(self, monkeypatch):
        monkeypatch.setenv("GROQ_API_KEY", "g")
        monkeypatch.setattr(R, "fetch_catalog", lambda p, timeout=None: set())
        groq = R.by_name("groq")
        monkeypatch.setattr(daily_quota, "spent",
                            lambda b: groq.daily_requests - R.SETTLEMENT_RESERVE)
        assert R.lane_providers(R.FILTER) == []
        assert [p.name for p, _m in R.lane_providers(R.SETTLEMENT)] == ["groq"]


class TestLanes:
    def test_une_lane_inconnue_est_refusee(self):
        with pytest.raises(ValueError):
            R.route([], "lane-imaginaire")

    def test_la_lane_cjk_prefere_les_modeles_chinois(self):
        """Double emploi avec la mission 3 : un modèle chinois résout un nom
        d'équipe CJK mieux qu'un Llama généraliste, et pour rien."""
        noms = [p.name for p in R.REGISTRY if R.TRANSLATE_CJK in p.lanes]
        assert "zhipu" in noms and "modelscope" in noms

    def test_le_settlement_a_au_moins_deux_fournisseurs_au_registre(self):
        noms = [p.name for p in R.REGISTRY if R.SETTLEMENT in p.lanes]
        assert len(noms) >= 2

    def test_mistral_est_au_registre_pour_les_lanes_de_signaux(self):
        """L'INVERSE de la règle d'avant, et c'est voulu.

        Mistral vivait hors registre parce qu'il était le fournisseur unique
        de Wiz — un domaine de panne isolé. Wiz supprimé le 2026-08-26, son
        quota est réalloué à la recherche de signaux.

        Ce que ce test verrouille, c'est la RETENUE de cette réallocation :
        à 2 requêtes/minute, Mistral n'a rien à faire dans SETTLEMENT (dont
        la réserve doit répondre vite), ni dans SEARCH_READ — sa valeur pour
        Wiz était son connecteur `web_search`, dont le quota était épuisé au
        niveau du COMPTE. L'y enrôler promettrait une capacité inexistante.
        """
        m = R.by_name("mistral")
        assert m is not None, "Mistral doit être au registre depuis la suppression de Wiz"
        assert set(m.lanes) == {R.FILTER, R.ANALYZE}, m.lanes
        assert R.SETTLEMENT not in m.lanes and R.SEARCH_READ not in m.lanes
        assert m.rpm == 2, "palier gratuit Mistral — 2 req/min"

    def test_la_lane_wiz_nexiste_plus(self):
        """Elle était mono-fournisseur et portait une exception dans
        refresh_catalogues(). Une lane de moins, une exception de moins."""
        assert not hasattr(R, "WIZ")
        assert "wiz" not in R.LANES


class TestCacheAvantTout:
    def test_le_cache_court_circuite_le_routeur(self, monkeypatch):
        """Le cache de la mission 2 s'applique AVANT tout appel, quel que soit
        le fournisseur."""
        monkeypatch.setattr("core.ai_search._cache_get", lambda k: "en-cache")
        appels = []
        monkeypatch.setattr(R, "route", lambda *a, **k: appels.append(1) or ("x", "y"))
        text, prov = R.complete("prompt", R.ANALYZE)
        assert (text, prov) == ("en-cache", "cache")
        assert appels == []


class TestDocumentation:
    """Un fournisseur ajouté au registre sans clé documentée est un
    fournisseur que l'opérateur n'activera jamais."""

    def _env_example(self):
        from pathlib import Path
        return Path(__file__).resolve().parent.parent.joinpath(".env.example").read_text(encoding="utf-8")

    def test_chaque_cle_du_registre_est_documentee(self):
        txt = self._env_example()
        for p in R.REGISTRY:
            assert f"{p.env_key}=" in txt, p.env_key

    def test_les_fournisseurs_marques_le_sont_aussi_dans_env_example(self):
        """L'opérateur doit voir la restriction d'usage AVANT de créer le
        compte, pas après."""
        txt = self._env_example()
        for p in R.REGISTRY:
            if not p.terms_flag:
                continue
            i = txt.index(f"{p.env_key}=")
            assert "⚖️" in txt[max(0, i - 400):i], p.env_key

    def test_la_variable_du_seul_fournisseur_mort_nest_plus_proposee(self):
        """GITHUB_MODELS_TOKEN ne doit plus apparaître comme une variable à
        renseigner. CEREBRAS_API_KEY, elle, est de retour : son retrait
        reposait sur un 403 sans clé qui ne prouvait rien."""
        for line in self._env_example().splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            assert not stripped.startswith("GITHUB_MODELS_TOKEN=")


class TestGabaritsDURL:
    """Cloudflare porte son identifiant de compte DANS l'URL."""

    def test_les_variables_sont_substituees(self, monkeypatch):
        monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "abc123")
        cf = R.by_name("cloudflare")
        assert "abc123" in cf.resolved_base and "${" not in cf.resolved_base
        assert cf.chat_url.endswith("/ai/v1/chat/completions")

    def test_une_cle_sans_son_identifiant_de_compte_est_ignoree(self, monkeypatch):
        """Sinon on appellerait une URL contenant littéralement `${...}` et on
        logguerait un échec réseau incompréhensible."""
        monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "t")
        monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
        assert "cloudflare" not in [p.name for p in R.active_providers()]

    def test_avec_les_deux_le_fournisseur_est_actif(self, monkeypatch):
        monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "t")
        monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "abc123")
        assert "cloudflare" in [p.name for p in R.active_providers()]


class TestCouvertureDesLanes:
    def test_chaque_lane_de_production_a_au_moins_deux_fournisseurs(self):
        """Sinon elle alerterait en permanence — et une alerte permanente est
        une alerte qu'on n'ouvre plus."""
        for lane in R.LANES:
            n = [p for p in R.REGISTRY if lane in p.lanes and not p.terms_flag]
            assert len(n) >= R.LANE_MIN_HEALTHY, f"{lane}: {len(n)}"

    def test_gemini_ne_sert_jamais_la_lane_de_recherche(self):
        """Le grounding Google Search gratuit est MORT (limit:0, vérifié sur
        4 clés le 2026-07-21). Seule la génération simple survit."""
        assert R.SEARCH_READ not in R.by_name("gemini").lanes


class TestBasculeDeModeleIntraFournisseur:
    """Mesuré le 2026-08-22 : les modèles `:free` d'OpenRouter sont bridés EN
    AMONT, modèle par modèle et de façon fluctuante. Au même instant,
    `gemma-4-31b-it:free` rendait 429 pendant que `nemotron-3-nano-30b:free`
    répondait « OK ». N'essayer qu'un modèle par fournisseur jetait donc tout
    OpenRouter parce qu'UN de ses modèles était saturé."""

    def test_un_429_essaie_le_modele_suivant_du_meme_fournisseur(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "o")
        orp = R.by_name("openrouter")
        monkeypatch.setattr(R, "fetch_catalog",
                            lambda p, timeout=None: set(orp.models))
        vus = []

        def post(url, json=None, headers=None, timeout=None):
            vus.append(json["model"])
            return _R(status=429) if len(vus) == 1 else _R(text="OK")
        monkeypatch.setattr(R.requests, "post", post)
        text, prov = R.route([], R.ANALYZE)
        assert text == "OK" and prov == "openrouter"
        assert vus == list(orp.models[:2])       # a bien changé de modèle

    def test_une_401_ne_reessaie_PAS_les_autres_modeles(self, monkeypatch):
        """Une clé refusée l'est pour tous les modèles : insister brûlerait le
        budget pour rien."""
        monkeypatch.setenv("OPENROUTER_API_KEY", "o")
        monkeypatch.setattr(R, "fetch_catalog",
                            lambda p, timeout=None: set(R.by_name("openrouter").models))
        vus = []

        def post(url, json=None, headers=None, timeout=None):
            vus.append(json["model"]); return _R(status=401)
        monkeypatch.setattr(R.requests, "post", post)
        assert R.route([], R.ANALYZE) == (None, None)
        assert len(vus) == 1

    def test_resolve_models_garde_lordre_des_preferences(self):
        p = R.Provider(name="x", base_url="https://x/v1", env_key="X",
                       models=("a", "b", "c"), lanes=(R.ANALYZE,))
        assert R.resolve_models(p, {"c", "a"}) == ["a", "c"]

    def test_catalogue_muet_garde_toutes_les_preferences(self):
        p = R.Provider(name="x", base_url="https://x/v1", env_key="X",
                       models=("a", "b"), lanes=(R.ANALYZE,))
        assert R.resolve_models(p, set()) == ["a", "b"]


class TestParsageDeCatalogue:
    """Trois formes réelles rencontrées le 2026-08-22, chacune avait un piège."""

    def _cat(self, monkeypatch, body):
        R._catalog_cache.clear()
        monkeypatch.setattr(R.requests, "get", lambda *a, **k: type(
            "X", (), {"status_code": 200, "json": staticmethod(lambda: body)})())
        p = R.Provider(name="essai", base_url="https://x/v1", env_key="X",
                       models=("m",), lanes=(R.ANALYZE,))
        return R.fetch_catalog(p)

    def test_cloudflare_expose_un_id_UUID_ET_un_name(self, monkeypatch):
        """Préférer `id` indexait 64 UUID et concluait « aucune préférence au
        catalogue » sur un fournisseur parfaitement sain."""
        cat = self._cat(monkeypatch, {"result": [
            {"id": "3fd0a7e0-uuid", "name": "@cf/meta/llama-3.3-70b-instruct-fp8-fast"}]})
        assert "@cf/meta/llama-3.3-70b-instruct-fp8-fast" in cat
        assert "3fd0a7e0-uuid" in cat          # les deux sont acceptés

    def test_le_prefixe_models_de_gemini_est_normalise(self, monkeypatch):
        cat = self._cat(monkeypatch, {"data": [{"id": "models/gemini-2.5-flash"}]})
        assert {"models/gemini-2.5-flash", "gemini-2.5-flash"} <= cat

    def test_le_prefixe_arobase_de_cloudflare_nest_PAS_tronque(self, monkeypatch):
        """`@cf/...` fait partie intégrante de l'identifiant attendu à
        l'inférence — le tronquer fabriquerait un modèle inexistant."""
        cat = self._cat(monkeypatch, {"result": [{"name": "@cf/zai-org/glm-4.7-flash"}]})
        assert "@cf/zai-org/glm-4.7-flash" in cat
        assert "glm-4.7-flash" not in cat


class TestModeleDisparu:
    def test_un_410_essaie_le_modele_suivant(self, monkeypatch):
        """Cloudflare rend 410 « deprecated on 2026-05-30 » sur un modèle
        retiré : c'est une raison d'essayer le suivant, pas d'abandonner le
        fournisseur."""
        assert 410 in R._RETRY_NEXT_MODEL and 404 in R._RETRY_NEXT_MODEL


class TestQuatreCentTrois:
    """Un 403 ne veut pas toujours dire « clé refusée ».

    Mesuré le 2026-08-22 sur Ollama Cloud AVEC UNE CLÉ VALIDE : `glm-5.2` rend
    403 « this model requires a subscription » pendant que `gpt-oss:120b`
    répond « OK ». Traiter ce 403 comme une clé refusée écartait tout le
    fournisseur — et avec lui la moitié de la lane settlement.
    """

    def test_un_403_essaie_le_modele_suivant(self, monkeypatch):
        monkeypatch.setenv("OLLAMA_API_KEY", "k")
        oll = R.by_name("ollama_cloud")
        monkeypatch.setattr(R, "fetch_catalog", lambda p, timeout=None: set(oll.models))
        vus = []

        def post(url, json=None, headers=None, timeout=None):
            vus.append(json["model"])
            return _R(status=403, text="requires a subscription") if len(vus) == 1 \
                else _R(text="OK")
        monkeypatch.setattr(R.requests, "post", post)
        text, prov = R.route([], R.SETTLEMENT)
        assert text == "OK" and prov == "ollama_cloud"
        assert len(vus) == 2

    def test_une_cle_invalide_reste_bornee_par_le_disjoncteur(self, monkeypatch):
        """Le prix à payer pour inclure 403 : sur une clé réellement refusée on
        épuise la liste. Borné — chaque tentative compte comme un échec."""
        monkeypatch.setenv("OLLAMA_API_KEY", "k")
        oll = R.by_name("ollama_cloud")
        monkeypatch.setattr(R, "fetch_catalog", lambda p, timeout=None: set(oll.models))
        vus = []
        monkeypatch.setattr(R.requests, "post",
                            lambda url, json=None, headers=None, timeout=None:
                            vus.append(json["model"]) or _R(status=403))
        assert R.route([], R.SETTLEMENT) == (None, None)
        assert len(vus) == len(oll.models)
        assert R.load_health("ollama_cloud")["consecutive_errors"] >= R.BREAKER_THRESHOLD

    def test_le_palier_gratuit_dollama_est_en_tete(self):
        """gpt-oss:120b est le seul des préférences accessible sans
        abonnement : le mettre ailleurs qu'en tête écarterait le fournisseur
        à chaque run."""
        assert R.by_name("ollama_cloud").models[0] == "gpt-oss:120b"

    def test_la_lane_settlement_a_bien_deux_fournisseurs_de_production(self):
        n = [p.name for p in R.REGISTRY if R.SETTLEMENT in p.lanes and not p.terms_flag]
        assert len(n) >= R.LANE_MIN_HEALTHY, n


class TestBudgetsRealistes:
    def test_le_budget_ne_depasse_pas_le_plafond_du_fournisseur(self):
        """OpenRouter free tier ≈ 50 req/j (confirmé par /key : is_free_tier).
        Un budget PREDATOR supérieur ne récolterait que des 429 : on veut
        basculer AVANT de se faire couper, pas après."""
        assert R.by_name("openrouter").daily_requests <= 50


class TestRepartitionSur24h:
    """La capacité ajoutée ne sert que si elle est APPELÉE.

    Suivre l'ordre du registre draine toujours le même fournisseur : sa
    réserve part en quelques heures pendant que les autres restent intacts.
    Le tri par budget restant est ce qui transforme « 11 fournisseurs
    configurés » en « 11 fournisseurs utilisés ».
    """

    def _trois(self, monkeypatch, spent):
        for n in ("GEMINI_API_KEY", "CLOUDFLARE_API_TOKEN",
                  "CLOUDFLARE_ACCOUNT_ID", "OPENROUTER_API_KEY"):
            monkeypatch.setenv(n, "x")
        monkeypatch.setattr(R, "fetch_catalog", lambda p, timeout=None: set())
        monkeypatch.setattr(daily_quota, "spent", lambda b: spent.get(b, 0))

    def test_le_moins_servi_passe_en_premier(self, monkeypatch):
        self._trois(monkeypatch, {"ai_gemini": 190, "ai_cloudflare": 10,
                                  "ai_openrouter": 20})
        ordre = [p.name for p, _ in R.lane_providers(R.ANALYZE)]
        assert ordre[0] == "cloudflare"          # 10/200 consommé
        assert ordre[-1] == "gemini"             # 190/200 consommé

    def test_lordre_du_registre_departage_a_egalite(self, monkeypatch):
        """Quand personne n'est en avance, la préférence de qualité reprend
        la main — le tri équilibré ne la remplace pas, il l'arbitre."""
        self._trois(monkeypatch, {})
        ordre = [p.name for p, _ in R.lane_providers(R.ANALYZE)]
        rang = {p.name: i for i, p in enumerate(R.REGISTRY)}
        assert ordre == sorted(ordre, key=lambda n: rang[n])

    def test_un_fournisseur_en_avance_sur_la_cadence_passe_en_dernier(self, monkeypatch):
        """À 25 % de la journée, avoir consommé 90 % de son budget = en
        avance. On ne l'exclut pas — l'exclure à 00 h 05 bloquerait tout."""
        from datetime import datetime, timezone
        self._trois(monkeypatch, {"ai_gemini": 180})
        monkeypatch.setattr(R, "_now",
                            lambda: datetime(2026, 8, 22, 6, 0, tzinfo=timezone.utc))
        ordre = [p.name for p, _ in R.lane_providers(R.ANALYZE)]
        assert ordre[-1] == "gemini"
        assert "gemini" in ordre                 # relégué, jamais écarté

    def test_la_journee_est_bien_un_cycle_de_24h_UTC(self):
        from datetime import datetime, timezone
        minuit = datetime(2026, 8, 22, 0, 0, tzinfo=timezone.utc)
        midi = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
        presque = datetime(2026, 8, 22, 23, 59, tzinfo=timezone.utc)
        assert R._day_fraction(minuit) < 0.05
        assert abs(R._day_fraction(midi) - 0.5) < 0.01
        assert R._day_fraction(presque) > 0.99

    def test_desactiver_lequilibrage_restaure_lordre_du_registre(self, monkeypatch):
        self._trois(monkeypatch, {"ai_gemini": 190})
        rang = {p.name: i for i, p in enumerate(R.REGISTRY)}
        ordre = [p.name for p, _ in R.lane_providers(R.ANALYZE, balanced=False)]
        assert ordre == sorted(ordre, key=lambda n: rang[n])


class TestGroqEnReserve:
    def test_ai_complete_interroge_le_routeur_AVANT_groq(self, monkeypatch):
        """Groq est le SEUL à porter compound-mini (recherche web intégrée).
        Chaque token dépensé en complétion simple est retiré à
        `ai_search_complete` — le manque exact qui a bloqué le settlement le
        2026-08-02."""
        from core import ai_search
        appels = []
        monkeypatch.setattr(ai_search, "_cache_get", lambda k: None)
        monkeypatch.setattr(ai_search, "_cache_put", lambda k, t: None)
        monkeypatch.setattr(ai_search, "_groq_post",
                            lambda *a, **k: appels.append("groq") or "de-groq")
        monkeypatch.setattr(ai_search, "_fallback_post",
                            lambda *a, **k: appels.append("routeur") or "du-routeur")
        assert ai_search.ai_complete("q") == "du-routeur"
        assert appels == ["routeur"]             # Groq pas touché

    def test_groq_reprend_la_main_si_le_routeur_est_muet(self, monkeypatch):
        from core import ai_search
        appels = []
        monkeypatch.setattr(ai_search, "_cache_get", lambda k: None)
        monkeypatch.setattr(ai_search, "_cache_put", lambda k, t: None)
        monkeypatch.setattr(ai_search, "_fallback_post",
                            lambda *a, **k: appels.append("routeur") or None)
        monkeypatch.setattr(ai_search, "_groq_post",
                            lambda *a, **k: appels.append("groq") or "de-groq")
        assert ai_search.ai_complete("q") == "de-groq"
        assert appels == ["routeur", "groq"]

    def test_la_recherche_web_garde_groq_en_premier(self, monkeypatch):
        """`ai_search_complete` est l'inverse : compound-mini d'abord, parce
        qu'aucun autre fournisseur ne fait de recherche web."""
        import inspect
        from core import ai_search
        src = inspect.getsource(ai_search.ai_search_complete)
        assert src.index("_SEARCH_MODEL") < src.index("_fallback_post")


class TestCalibration24h:
    def test_la_charge_se_repartit_proportionnellement_aux_budgets(self, monkeypatch):
        """Simulation d'un cycle complet : 40 scans × 6 appels sur 24 h.

        Le critère n'est pas « tout le monde est servi » mais « chacun est
        servi À LA HAUTEUR DE SON BUDGET ». Sans ce tri, les 240 appels
        partaient intégralement sur le premier fournisseur du registre :
        sa réserve s'épuisait pendant que les autres restaient intacts.
        """
        from datetime import datetime, timedelta, timezone
        for n in ("GEMINI_API_KEY", "CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID",
                  "OPENROUTER_API_KEY", "OLLAMA_API_KEY", "GROQ_API_KEY"):
            monkeypatch.setenv(n, "x")
        monkeypatch.setattr(R, "fetch_catalog", lambda p, timeout=None: set())
        spent = {}
        monkeypatch.setattr(daily_quota, "spent", lambda b: spent.get(b, 0))
        base = datetime(2026, 8, 22, 0, 0, tzinfo=timezone.utc)
        horloge = [base]
        monkeypatch.setattr(R, "_now", lambda: horloge[0])

        servis = {}
        for i in range(40):
            horloge[0] = base + timedelta(minutes=36 * i)
            for _ in range(6):
                cands = R.lane_providers(R.ANALYZE)
                assert cands, "plus aucun fournisseur en cours de journée"
                prov = cands[0][0]
                spent[prov.bucket] = spent.get(prov.bucket, 0) + 1
                servis[prov.name] = servis.get(prov.name, 0) + 1

        assert sum(servis.values()) == 240
        assert len(servis) >= 5              # tout le monde a servi

        # Chacun consomme la MÊME fraction de son budget, à 10 points près.
        parts = []
        for nom, n in servis.items():
            p = R.by_name(nom)
            plafond = p.daily_requests - (R.SETTLEMENT_RESERVE
                                          if R.SETTLEMENT in p.lanes else 0)
            parts.append(n / plafond)
        assert max(parts) - min(parts) < 0.10, dict(zip(servis, parts))

    def test_le_budget_groq_reflete_son_TPD_et_non_un_nombre_de_requetes(self):
        """La contrainte de Groq est 100 000 tokens/jour PAR ORGANISATION.
        À ~600 tokens l'appel, cela fait ~165 appels — pas 400. Un budget
        surévalué ferait continuer d'appeler après l'épuisement du TPD, et
        surtout brûlerait sur des complétions simples le quota dont
        compound-mini a besoin."""
        groq = R.by_name("groq")
        assert groq.daily_tokens == 100_000
        assert groq.daily_requests <= groq.daily_tokens // 600


class TestAucunModeleEnDurHorsDuRegistre:
    """Gardien de la panne « listes qui divergent » (CLAUDE.md).

    Le 2026-08-26, `llama-3.3-70b-versatile` et `llama-3.1-8b-instant` ont
    disparu du catalogue Groq. `core/ai_router.py` a fait son travail — il a
    écarté Groq avec un log explicite. Mais `core/ai_search.py` portait TROIS
    copies de ces noms (`_TIER_MODELS`, `_EXTRACT_MODELS`, `_ALL_MODELS`) et
    appelait Groq EN DIRECT, sans passer par le routeur : 404 en boucle,
    backoff, puis le timeout global de 540 s qui tuait le Deep Scan du matin.

    Le gardien du registre existait déjà ; ce qui manquait, c'est qu'il
    s'applique au VRAI chemin d'appel. D'où ces deux tests.
    """

    def test_ai_search_derive_ses_modeles_du_registre(self):
        """Aucune des trois listes n'est recopiée : toutes viennent d'ici."""
        from core import ai_search

        attendus = list(R.by_name("groq").models)
        assert ai_search._groq_models() == attendus
        assert ai_search._all_models() == [ai_search._SEARCH_MODEL] + attendus
        # Le palier ne réordonne plus : inverser mettrait un modèle de
        # RAISONNEMENT en tête, qui rend un contenu vide sous max_tokens=80
        # (mesuré le 2026-08-26) — l'estimateur et les alias échoueraient
        # en silence.
        for tier in ("light", "heavy", "inconnu"):
            assert ai_search._tier_models(tier) == attendus, tier

    def test_aucun_nom_de_modele_code_en_dur_ailleurs(self):
        """« NE JAMAIS coder un nom de modèle en dur hors du registre. »

        Vérifié sur les LITTÉRAUX (via l'AST), pas sur le texte : les
        commentaires ont le droit de nommer un modèle mort pour raconter
        pourquoi il l'est.
        """
        import ast
        import glob
        import io
        import os

        ids = {m for p in R.REGISTRY for m in p.models}
        racine = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        coupables = {}
        for motif in ("core/*.py", "*.py", "scripts/*.py"):
            for f in sorted(glob.glob(os.path.join(racine, motif))):
                if os.path.basename(f) == "ai_router.py":
                    continue
                arbre = ast.parse(io.open(f, encoding="utf-8").read(), filename=f)
                litteraux = {n.value for n in ast.walk(arbre)
                             if isinstance(n, ast.Constant)
                             and isinstance(n.value, str)}
                if ids & litteraux:
                    coupables[os.path.relpath(f, racine)] = sorted(ids & litteraux)
        assert not coupables, (
            "modèle(s) du registre recopié(s) en dur — dérive la liste ou "
            f"passe par le routeur : {coupables}")
