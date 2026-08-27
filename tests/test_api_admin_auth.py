"""
tests/test_api_admin_auth.py — /api/audit/run était ouverte à tout Internet.

CE QUI A ÉTÉ MESURÉ (2026-08-22)

`POST /api/audit/run` déclenchait `audit.yml` — 45 minutes de runner, le
settlement complet, et la consommation de la réserve IA que
`core/ai_router.py` garde en négatif exprès — sans la moindre
authentification, sans cooldown, sans limite de débit. Le dashboard est servi
depuis une URL Vercel publique, et AUCUNE interface du dépôt n'appelle cette
route : elle n'était connue que du README. Une boucle `curl` anonyme suffisait
donc à épuiser le quota GitHub Actions et le quota IA du projet.

Ce que ça coûte n'est pas théorique : CLAUDE.md documente l'incident du
10→20 août 2026 — dix jours sans signal après un épuisement de quota.

La règle retenue est l'ÉCHEC FERMÉ. Sans `DASHBOARD_ADMIN_TOKEN` configuré,
la route refuse. C'est le point important : une protection qui s'ouvre quand
sa configuration manque ne protège rien, et c'est exactement la forme du bug
d'origine (« si pas de PAT → 503 », donc « si PAT → ouvert à tous »).
"""
import pytest

from api.index import ADMIN_TOKEN_ENV, app

JETON = "jeton-de-test-0123456789"


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


class TestAuditRun:
    def test_sans_jeton_configure_la_route_refuse(self, client, monkeypatch):
        """ÉCHEC FERMÉ — le cœur de la correction."""
        monkeypatch.delenv(ADMIN_TOKEN_ENV, raising=False)
        monkeypatch.setenv("GITHUB_PAT", "pat-bidon")
        assert client.post("/api/audit/run").status_code == 401

    def test_sans_en_tete_la_route_refuse(self, client, monkeypatch):
        monkeypatch.setenv(ADMIN_TOKEN_ENV, JETON)
        assert client.post("/api/audit/run").status_code == 401

    def test_mauvais_jeton_refuse(self, client, monkeypatch):
        monkeypatch.setenv(ADMIN_TOKEN_ENV, JETON)
        r = client.post("/api/audit/run", headers={"X-Predator-Token": "faux"})
        assert r.status_code == 401

    def test_bon_prefixe_mais_jeton_tronque_refuse(self, client, monkeypatch):
        """Un préfixe correct ne doit rien valoir — sinon le jeton se
        devine octet par octet."""
        monkeypatch.setenv(ADMIN_TOKEN_ENV, JETON)
        r = client.post("/api/audit/run",
                        headers={"X-Predator-Token": JETON[:-1]})
        assert r.status_code == 401

    def test_la_reponse_ne_dit_pas_pourquoi(self, client, monkeypatch):
        """« jeton non configuré » et « mauvais jeton » doivent être
        indiscernables : la différence n'aide que celui qui cherche à
        entrer."""
        monkeypatch.delenv(ADMIN_TOKEN_ENV, raising=False)
        sans_config = client.post("/api/audit/run").get_json()
        monkeypatch.setenv(ADMIN_TOKEN_ENV, JETON)
        mauvais = client.post("/api/audit/run",
                              headers={"X-Predator-Token": "faux"}).get_json()
        assert sans_config == mauvais

    def test_bon_jeton_franchit_lauthentification(self, client, monkeypatch):
        """Avec le bon jeton on passe l'authentification. On s'arrête au
        503 « GITHUB_PAT non configuré » : la preuve que le contrôle est
        franchi, sans qu'aucun appel réseau ne parte vers GitHub."""
        monkeypatch.setenv(ADMIN_TOKEN_ENV, JETON)
        monkeypatch.delenv("GITHUB_PAT", raising=False)
        r = client.post("/api/audit/run", headers={"X-Predator-Token": JETON})
        assert r.status_code == 503

    def test_le_jeton_en_query_string_est_REFUSE(self, client, monkeypatch):
        """C1 (2026-08-27) — le jeton en `?token=` était accepté « pour un
        curl d'opérateur », documenté comme moins sûr, et accepté quand même.

        Ce que « moins sûr » recouvrait : une URL est écrite en clair dans les
        logs d'accès de Vercel, ceux du proxy, l'historique du shell, l'en-tête
        `Referer` envoyé à tout tiers, et l'historique du navigateur. Ces
        journaux SURVIVENT au jeton — une rotation ne les efface pas. Un
        en-tête n'apparaît dans aucun de ces endroits.

        Le BON jeton, par le MAUVAIS canal, doit être refusé : c'est le canal
        qui est condamné, pas la valeur.
        """
        monkeypatch.setenv(ADMIN_TOKEN_ENV, JETON)
        monkeypatch.delenv("GITHUB_PAT", raising=False)
        assert client.post(f"/api/audit/run?token={JETON}").status_code == 401

    def test_len_tete_reste_le_seul_canal_accepte(self, client, monkeypatch):
        """Témoin : sans lui, le test ci-dessus passerait même si la route
        refusait TOUT."""
        monkeypatch.setenv(ADMIN_TOKEN_ENV, JETON)
        monkeypatch.delenv("GITHUB_PAT", raising=False)
        r = client.post(f"/api/audit/run?token={JETON}",
                        headers={"X-Predator-Token": JETON})
        assert r.status_code == 503, \
            "une query string parasite ne doit pas invalider un en-tête correct"

    def test_la_query_string_ne_sert_pas_de_repli_sur_en_tete_errone(self, client,
                                                                    monkeypatch):
        """Le piège du `or` : `en_tete or query` faisait retomber sur la query
        string dès que l'en-tête était vide OU faux."""
        monkeypatch.setenv(ADMIN_TOKEN_ENV, JETON)
        r = client.post(f"/api/audit/run?token={JETON}",
                        headers={"X-Predator-Token": "faux"})
        assert r.status_code == 401

    def test_le_refus_par_query_string_est_journalise(self, client, monkeypatch,
                                                      caplog):
        """Muet côté client, BRUYANT côté serveur : l'opérateur dont le vieux
        `curl` ne passe plus doit comprendre pourquoi en lisant les logs."""
        import logging
        monkeypatch.setenv(ADMIN_TOKEN_ENV, JETON)
        with caplog.at_level(logging.WARNING):
            client.post(f"/api/audit/run?token={JETON}")
        assert any("query string" in r.getMessage() for r in caplog.records)

    def test_le_jeton_nest_jamais_recopie_dans_le_log(self, client, monkeypatch,
                                                      caplog):
        """Journaliser le refus ne doit pas journaliser le secret — sinon on
        déplace la fuite au lieu de la fermer."""
        import logging
        monkeypatch.setenv(ADMIN_TOKEN_ENV, JETON)
        with caplog.at_level(logging.WARNING):
            client.post(f"/api/audit/run?token={JETON}")
        assert all(JETON not in r.getMessage() for r in caplog.records)

    def test_get_reste_interdit(self, client, monkeypatch):
        monkeypatch.setenv(ADMIN_TOKEN_ENV, JETON)
        assert client.get("/api/audit/run").status_code == 405


class TestPasDeFuiteDansLesReponses:
    """Un message d'erreur brut nomme la table, la colonne, la politique RLS
    ou la portée du jeton. Le détail appartient au log du déploiement."""

    def test_api_signals_ne_recopie_pas_lexception(self, client, monkeypatch):
        import api.index as idx

        def _boom(*a, **k):
            raise RuntimeError("permission denied for table signals (RLS xyz)")

        monkeypatch.setattr(idx, "_db", _boom)
        r = client.get("/api/signals")
        assert r.status_code == 500
        assert "RLS" not in r.get_data(as_text=True)
        assert "signals" not in (r.get_json() or {}).get("error", "")


class TestSondeDeSante:
    def test_health_repond_sans_base(self, client, monkeypatch):
        """Sonde de disponibilité : elle doit répondre même base injoignable
        — sinon elle mesure Supabase, pas le dashboard."""
        monkeypatch.delenv("SUPABASE_URL", raising=False)
        r = client.get("/api/health")
        assert r.status_code == 200
        assert r.get_json()["db_configured"] is False

    def test_health_ne_publie_aucun_secret(self, client, monkeypatch):
        monkeypatch.setenv("SUPABASE_URL", "https://exemple.supabase.co")
        monkeypatch.setenv("SUPABASE_KEY", "cle-tres-secrete-42")
        corps = client.get("/api/health").get_data(as_text=True)
        assert "cle-tres-secrete-42" not in corps
        assert corps.count("supabase.co") == 0

    def test_health_porte_la_version_unique(self, client):
        from api.index import DASHBOARD_VERSION
        assert client.get("/api/health").get_json()["version"] == DASHBOARD_VERSION


# ── C2 — /api/scan était ouverte à tout Internet ─────────────────────────

class TestScanLimiteDeDebit:
    """`POST /api/scan` pose `meta.scan_request` et n'exigeait RIEN. Le
    dashboard est servi depuis une URL Vercel publique.

    Le cooldown global de 120 s bornait déjà la fréquence d'ÉCRITURE, mais
    chaque requête refusée coûtait quand même la création d'un client
    service_role et une lecture de `meta` — et rien n'empêchait de maintenir
    un scan perpétuellement en attente, donc de forcer un scan complet à
    chaque tick de cron.

    Pourquoi une limite de débit et NON un jeton : la route est appelée par un
    bouton public du dashboard (`templates/index.html`, `triggerScan`). Un
    jeton y serait écrit dans le JavaScript servi à tout le monde — pas une
    protection, l'illusion d'une.
    """

    @pytest.fixture(autouse=True)
    def _compteur_vierge(self):
        from api.index import _scan_hits
        _scan_hits.clear()
        yield
        _scan_hits.clear()

    def test_les_premieres_demandes_passent(self, client, monkeypatch):
        from api.index import _SCAN_RATE_LIMIT_N
        monkeypatch.delenv(ADMIN_TOKEN_ENV, raising=False)
        for i in range(_SCAN_RATE_LIMIT_N):
            r = client.post("/api/scan", headers={"x-vercel-forwarded-for": "1.2.3.4"})
            assert r.status_code != 429, f"demande {i + 1} refusée trop tôt"

    def test_au_dela_du_quota_la_route_refuse(self, client, monkeypatch):
        from api.index import _SCAN_RATE_LIMIT_N
        monkeypatch.delenv(ADMIN_TOKEN_ENV, raising=False)
        for _ in range(_SCAN_RATE_LIMIT_N):
            client.post("/api/scan", headers={"x-vercel-forwarded-for": "1.2.3.4"})
        r = client.post("/api/scan", headers={"x-vercel-forwarded-for": "1.2.3.4"})
        assert r.status_code == 429
        assert r.get_json()["status"] == "rate_limited"

    def test_le_refus_precede_tout_acces_a_la_base(self, client, monkeypatch):
        """L'intérêt principal : une requête abusive ne doit plus coûter une
        lecture Supabase. Sans cela, la limite ne fait qu'économiser
        l'écriture, qui était déjà bornée par le cooldown."""
        import api.index as idx
        from api.index import _SCAN_RATE_LIMIT_N
        monkeypatch.delenv(ADMIN_TOKEN_ENV, raising=False)
        for _ in range(_SCAN_RATE_LIMIT_N):
            client.post("/api/scan", headers={"x-vercel-forwarded-for": "9.9.9.9"})

        def _interdit(*_a, **_k):
            raise AssertionError("_db() appelé alors que la limite était atteinte")

        monkeypatch.setattr(idx, "_db", _interdit)
        assert client.post("/api/scan",
                           headers={"x-vercel-forwarded-for": "9.9.9.9"}).status_code == 429

    def test_deux_IP_ont_des_compteurs_SEPARES(self, client, monkeypatch):
        """Sinon le premier visiteur bloquerait tous les autres — c'est ce qui
        arriverait en comptant sur `remote_addr`, identique pour tout le monde
        derrière le proxy Vercel."""
        from api.index import _SCAN_RATE_LIMIT_N
        monkeypatch.delenv(ADMIN_TOKEN_ENV, raising=False)
        for _ in range(_SCAN_RATE_LIMIT_N + 2):
            client.post("/api/scan", headers={"x-vercel-forwarded-for": "1.1.1.1"})
        r = client.post("/api/scan", headers={"x-vercel-forwarded-for": "2.2.2.2"})
        assert r.status_code != 429

    def test_lIP_est_lue_sur_len_tete_de_la_plateforme_en_priorite(self):
        """`x-vercel-forwarded-for` est posé par Vercel et n'est pas
        falsifiable ; `x-forwarded-for` l'est. Le premier doit primer."""
        from api.index import _client_ip, app as flask_app
        with flask_app.test_request_context(
                headers={"x-vercel-forwarded-for": "5.5.5.5",
                         "x-forwarded-for": "6.6.6.6"}):
            assert _client_ip() == "5.5.5.5"

    def test_seule_lIP_dorigine_est_retenue_dans_la_chaine(self):
        """Un `x-forwarded-for` porte « client, relais1, relais2 ». Compter sur
        le dernier ferait partager un compteur à tous les clients d'un même
        relais."""
        from api.index import _client_ip, app as flask_app
        with flask_app.test_request_context(
                headers={"x-forwarded-for": "7.7.7.7, 10.0.0.1, 10.0.0.2"}):
            assert _client_ip() == "7.7.7.7"

    def test_la_fenetre_glisse(self):
        """Une IP bloquée doit redevenir libre — sinon la limite est un
        bannissement définitif, et le bouton du dashboard cesse de marcher
        pour un visiteur ordinaire."""
        from api.index import (_SCAN_RATE_LIMIT_N, _SCAN_RATE_LIMIT_WINDOW_S,
                               _scan_rate_limited)
        t = 1000.0
        for _ in range(_SCAN_RATE_LIMIT_N):
            assert _scan_rate_limited("8.8.8.8", t) is False
        assert _scan_rate_limited("8.8.8.8", t) is True
        assert _scan_rate_limited("8.8.8.8", t + _SCAN_RATE_LIMIT_WINDOW_S + 1) is False

    def test_le_jeton_dadmin_dispense_de_la_limite(self, client, monkeypatch):
        """Le seul appelant capable de garder un secret. Le bouton du
        dashboard, lui, est servi à tout le monde."""
        from api.index import _SCAN_RATE_LIMIT_N
        monkeypatch.setenv(ADMIN_TOKEN_ENV, JETON)
        for _ in range(_SCAN_RATE_LIMIT_N + 3):
            r = client.post("/api/scan",
                            headers={"X-Predator-Token": JETON,
                                     "x-vercel-forwarded-for": "3.3.3.3"})
            assert r.status_code != 429

    def test_un_mauvais_jeton_ne_dispense_de_rien(self, client, monkeypatch):
        from api.index import _SCAN_RATE_LIMIT_N
        monkeypatch.setenv(ADMIN_TOKEN_ENV, JETON)
        for _ in range(_SCAN_RATE_LIMIT_N):
            client.post("/api/scan", headers={"X-Predator-Token": "faux",
                                              "x-vercel-forwarded-for": "4.4.4.4"})
        r = client.post("/api/scan", headers={"X-Predator-Token": "faux",
                                              "x-vercel-forwarded-for": "4.4.4.4"})
        assert r.status_code == 429

    def test_le_compteur_ne_grandit_pas_indefiniment(self):
        """Une instance chaude de longue durée verrait le dictionnaire enfler
        d'une entrée par IP vue, sans jamais rien libérer."""
        from api.index import _SCAN_RATE_LIMIT_WINDOW_S, _scan_hits, _scan_rate_limited
        _scan_hits.clear()
        for i in range(50):
            _scan_rate_limited(f"10.0.0.{i}", 1000.0)
        assert len(_scan_hits) == 50
        _scan_rate_limited("11.0.0.1", 1000.0 + _SCAN_RATE_LIMIT_WINDOW_S + 1)
        assert len(_scan_hits) == 1, "les fenêtres périmées doivent être purgées"


# ── C3 — le dashboard n'a plus aucune clé d'écriture ─────────────────────

class _RpcSB:
    """Faux client Supabase qui n'expose QUE `rpc`. Toute tentative
    d'écriture directe lève : c'est le contrat de C3."""

    def __init__(self, reponse):
        self.reponse = reponse
        self.appels = []

    def rpc(self, nom, params=None):
        self.appels.append((nom, params))
        rep = self.reponse

        class _Q:
            def execute(self):
                if isinstance(rep, Exception):
                    raise rep
                return type("R", (), {"data": rep})()

        return _Q()

    def table(self, nom):
        raise AssertionError(
            f"écriture directe sur {nom} — le dashboard ne doit plus en faire")


class TestScanPasseParLaFonctionPostgres:
    """C3 — `/api/scan` était la SEULE écriture du dashboard, et elle exigeait
    la clé service_role : les pleins pouvoirs sur `signals`,
    `ai_learning_ledger`, `meta` et `app_secrets` pour une fonction servie
    publiquement. Elle passe par `demander_scan()`, fonction Postgres
    `security definer` appelable avec la clé de lecture."""

    @pytest.fixture(autouse=True)
    def _compteur_vierge(self):
        from api.index import _scan_hits
        _scan_hits.clear()
        yield
        _scan_hits.clear()

    def _cabler(self, monkeypatch, reponse):
        import api.index as idx
        faux = _RpcSB(reponse)
        monkeypatch.setattr(idx, "_db", lambda write=False: faux)
        monkeypatch.delenv(ADMIN_TOKEN_ENV, raising=False)
        return faux

    def test_la_route_appelle_la_fonction_et_lui_passe_lIP(self, client, monkeypatch):
        faux = self._cabler(monkeypatch, {"status": "queued", "message": "ok"})
        r = client.post("/api/scan", headers={"x-vercel-forwarded-for": "1.2.3.4"})
        assert r.status_code == 200
        assert faux.appels == [("demander_scan", {"p_ip": "1.2.3.4"})]

    def test_le_client_demande_est_celui_de_LECTURE(self, monkeypatch, client):
        """Si la route redemandait `write=True`, retirer la clé de Vercel la
        casserait — et tout C3 serait annulé sans qu'aucun test ne bronche."""
        import api.index as idx
        vus = []
        faux = _RpcSB({"status": "queued"})

        def _db(write=False):
            vus.append(write)
            return faux

        monkeypatch.setattr(idx, "_db", _db)
        monkeypatch.delenv(ADMIN_TOKEN_ENV, raising=False)
        client.post("/api/scan", headers={"x-vercel-forwarded-for": "1.2.3.4"})
        assert vus == [False], f"le dashboard a demandé une clé d'écriture : {vus}"

    def test_aucune_ecriture_directe_nest_tentee(self, client, monkeypatch):
        # `_RpcSB.table` lève : le test échouerait si la route écrivait encore.
        self._cabler(monkeypatch, {"status": "queued"})
        assert client.post("/api/scan",
                           headers={"x-vercel-forwarded-for": "1.2.3.4"}).status_code == 200

    @pytest.mark.parametrize("statut,code", [
        ("queued", 200), ("already_queued", 429), ("rate_limited", 429),
        ("error", 500),
    ])
    def test_chaque_statut_a_son_code_http(self, client, monkeypatch, statut, code):
        self._cabler(monkeypatch, {"status": statut, "message": "m"})
        r = client.post("/api/scan", headers={"x-vercel-forwarded-for": "5.6.7.8"})
        assert r.status_code == code

    def test_une_reponse_inattendue_ne_passe_pas_pour_un_succes(self, client,
                                                                monkeypatch):
        """Une fonction qui rendrait autre chose — parce qu'elle a été
        modifiée en base sans que le code suive — ne doit pas être lue comme
        un scan accepté."""
        self._cabler(monkeypatch, {"quelque_chose": "d'autre"})
        assert client.post("/api/scan",
                           headers={"x-vercel-forwarded-for": "5.6.7.8"}).status_code == 500

    def test_une_panne_de_la_fonction_ne_fuit_pas_dans_la_reponse(self, client,
                                                                  monkeypatch):
        """Un message PostgREST brut nomme la fonction, le schéma et la
        politique qui a refusé."""
        self._cabler(monkeypatch, RuntimeError(
            'permission denied for function demander_scan, policy "meta_service_update"'))
        r = client.post("/api/scan", headers={"x-vercel-forwarded-for": "5.6.7.8"})
        assert r.status_code == 500
        corps = r.get_data(as_text=True)
        assert "demander_scan" not in corps and "policy" not in corps

    def test_la_limite_en_memoire_refuse_AVANT_douvrir_une_connexion(self, client,
                                                                    monkeypatch):
        """C2 est conservée EN AMONT de C3 : elle refuse sans même joindre la
        base, ce que le SQL ne peut pas faire par construction."""
        import api.index as idx
        from api.index import _SCAN_RATE_LIMIT_N
        faux = self._cabler(monkeypatch, {"status": "queued"})
        for _ in range(_SCAN_RATE_LIMIT_N):
            client.post("/api/scan", headers={"x-vercel-forwarded-for": "7.7.7.7"})
        avant = len(faux.appels)
        monkeypatch.setattr(idx, "_db",
                            lambda write=False: (_ for _ in ()).throw(
                                AssertionError("base jointe malgré la limite")))
        assert client.post("/api/scan",
                           headers={"x-vercel-forwarded-for": "7.7.7.7"}).status_code == 429
        assert len(faux.appels) == avant


class TestLeDashboardNaPlusDeCleDEcriture:
    """Le garde qui compte : tant qu'un seul `write=True` subsiste, retirer
    `SUPABASE_SERVICE_KEY` de Vercel casse le dashboard en production."""

    def test_aucun_appel_a_une_cle_decriture_dans_le_dashboard(self):
        import ast
        import pathlib
        src = (pathlib.Path(__file__).resolve().parent.parent
               / "api" / "index.py").read_text(encoding="utf-8")
        coupables = []
        for noeud in ast.walk(ast.parse(src)):
            if not isinstance(noeud, ast.Call):
                continue
            nom = getattr(noeud.func, "id", None) or getattr(noeud.func, "attr", None)
            if nom not in ("_db", "get_db", "_get_db_client"):
                continue
            for kw in noeud.keywords:
                if kw.arg == "write" and getattr(kw.value, "value", False) is True:
                    coupables.append(noeud.lineno)
        assert coupables == [], \
            f"api/index.py demande encore une clé d'écriture, lignes {coupables}"

    def test_aucune_ecriture_supabase_directe_dans_le_dashboard(self):
        """`.upsert` / `.insert` / `.update` / `.delete` sur une table n'ont
        plus rien à faire ici : tout passe par la fonction Postgres."""
        import re
        import pathlib
        src = (pathlib.Path(__file__).resolve().parent.parent
               / "api" / "index.py").read_text(encoding="utf-8")
        code = [l for l in src.splitlines()
                if not l.strip().startswith("#") and "table(" in l]
        ecritures = [l.strip() for l in code
                     if re.search(r"\.(upsert|insert|update|delete)\(", l)]
        assert ecritures == [], ecritures
