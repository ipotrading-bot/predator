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
