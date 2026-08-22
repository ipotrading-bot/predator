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
        assert [p.name for p, _ in R.lane_providers(R.ANALYZE, allow_flagged=True)] == ["cohere"]


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
        assert [p.name for p, _ in R.lane_providers(R.ANALYZE)] == ["openrouter"]
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
        text, prov = R.route([], R.ANALYZE, validator=valide)
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
        assert [p.name for p, _ in R.lane_providers(R.SETTLEMENT)] == ["groq"]


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

    def test_wiz_nest_pas_servi_par_le_routeur(self):
        """Mistral reste hors registre : c'est le domaine de panne de Wiz."""
        assert [p for p in R.REGISTRY if R.WIZ in p.lanes] == []
        assert "mistral" not in {p.name for p in R.REGISTRY}


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
            if lane == R.WIZ:
                continue
            n = [p for p in R.REGISTRY if lane in p.lanes and not p.terms_flag]
            assert len(n) >= R.LANE_MIN_HEALTHY, f"{lane}: {len(n)}"

    def test_gemini_ne_sert_jamais_la_lane_de_recherche(self):
        """Le grounding Google Search gratuit est MORT (limit:0, vérifié sur
        4 clés le 2026-07-21). Seule la génération simple survit."""
        assert R.SEARCH_READ not in R.by_name("gemini").lanes
