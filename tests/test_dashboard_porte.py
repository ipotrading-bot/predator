"""
tests/test_dashboard_porte.py — le dashboard était lisible par tout Internet.

CE QUI A ÉTÉ MESURÉ (2026-11-17)

Sur la production, `/`, `/ledger`, `/audit`, `/performance`, `/system` et
`/api/signals` répondaient 200 sans rien demander, et l'URL figure dans un
dépôt public. Tout visiteur lisait les paris. Il pouvait aussi cliquer
« Scanner » : une demande manuelle promeut le tick horaire gratuit en scan
PAYANT sans passer par le garde « créneau déjà servi » de
`scripts/ci_scan_mode.py` — jusqu'à 24 scans payants forcés par jour.

La règle retenue est celle de `/api/audit/run` : ÉCHEC FERMÉ. Sans
`DASHBOARD_PASSWORD`, ou avec un mot de passe sous le plancher, personne
n'entre. Ces tests n'appellent que des routes qui REDIRIGENT ou refusent :
aucun gabarit n'est rendu (convention de la suite).
"""
import pytest

import api.index as dashboard
from api.index import (ADMIN_TOKEN_ENV, PASSWORD_ENV, PASSWORD_MIN,
                       _CHEMINS_PUBLICS, _SESSION_COOKIE, _signer_session, app)

pytestmark = pytest.mark.porte_reelle

MDP = "mot-de-passe-de-test"          # ≥ PASSWORD_MIN
PAGES = ["/", "/ledger", "/audit", "/performance", "/system"]
API = ["/api/signals", "/api/signals?all=1"]


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv(PASSWORD_ENV, MDP)
    monkeypatch.delenv(ADMIN_TOKEN_ENV, raising=False)
    monkeypatch.setattr(dashboard, "_login_hits", {})
    # Une route qui s'ouvrirait par erreur ne doit pas joindre de base.
    monkeypatch.setattr(dashboard, "_get_db_client", lambda write=False: None)
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def _cookie(reponse) -> str:
    return next((h for h in reponse.headers.getlist("Set-Cookie")
                 if h.startswith(_SESSION_COOKIE + "=")), "")


def _entrer(client):
    r = client.post("/login", data={"password": MDP})
    assert r.status_code == 303 and r.headers["Location"] == "/"
    return r


class TestPorteFermee:
    @pytest.mark.parametrize("page", PAGES)
    def test_une_page_renvoie_vers_la_connexion(self, client, page):
        r = client.get(page)
        assert r.status_code == 303
        assert r.headers["Location"] == "/login"

    @pytest.mark.parametrize("route", API)
    def test_une_api_rend_401_sans_donnees(self, client, route):
        r = client.get(route)
        assert r.status_code == 401
        assert r.get_json() == {"error": "non autorisé"}

    def test_le_bouton_scanner_est_ferme_aux_inconnus(self, client, monkeypatch):
        """Le cœur du risque : un POST anonyme ne doit plus atteindre
        `demander_scan`, donc plus promouvoir un tick en scan payant."""
        appels = []
        monkeypatch.setattr(dashboard, "_db", lambda write=False: appels.append(1))
        assert client.post("/api/scan").status_code == 401
        assert appels == []

    def test_toute_route_du_dashboard_est_derriere_la_porte(self, client):
        """Dérivé de la table de routes (règle n°6) : une route AJOUTÉE
        demain est fermée sans que personne ait à y penser — la porte est un
        `before_request`, pas un décorateur qu'on oublie."""
        ouvertes = []
        for regle in app.url_map.iter_rules():
            if "<" in regle.rule or regle.rule in _CHEMINS_PUBLICS:
                continue
            methode = "GET" if "GET" in regle.methods else "POST"
            r = client.open(regle.rule, method=methode)
            if r.status_code not in (303, 401):
                ouvertes.append((regle.rule, r.status_code))
        assert ouvertes == []

    def test_les_chemins_publics_ne_lisent_pas_la_base(self, client):
        """Ce qui reste ouvert doit rester sans donnée : la sonde, la porte,
        et les fichiers dont le navigateur a besoin avant de se connecter."""
        assert _CHEMINS_PUBLICS == {"/login", "/logout", "/api/health",
                                    "/manifest.json", "/favicon.ico"}
        r = client.get("/api/health")
        assert r.status_code == 200
        assert r.get_json()["auth_configured"] is True


class TestEchecFerme:
    def test_sans_mot_de_passe_configure_personne_nentre(self, client, monkeypatch):
        monkeypatch.delenv(PASSWORD_ENV, raising=False)
        r = client.post("/login", data={"password": ""})
        assert r.headers["Location"] == "/login?e=1" and not _cookie(r)
        assert client.get("/").status_code == 303
        assert client.get("/api/health").get_json()["auth_configured"] is False

    def test_un_mot_de_passe_trop_court_vaut_absent(self, client, monkeypatch):
        """La limite de débit vit dans une instance : c'est la longueur qui
        tient la porte. « 1234 » ne doit ouvrir à personne, pas même à
        celui qui le connaît."""
        court = "x" * (PASSWORD_MIN - 1)
        monkeypatch.setenv(PASSWORD_ENV, court)
        r = client.post("/login", data={"password": court})
        assert r.headers["Location"] == "/login?e=1" and not _cookie(r)

    def test_un_cookie_signe_sans_mot_de_passe_configure_ne_vaut_rien(self, client,
                                                                     monkeypatch):
        _entrer(client)
        monkeypatch.delenv(PASSWORD_ENV, raising=False)
        assert client.get("/api/signals").status_code == 401


class TestConnexion:
    def test_le_bon_mot_de_passe_ouvre(self, client):
        _entrer(client)
        # La porte est franchie : la route répond elle-même (503, pas de base).
        assert client.get("/api/signals").status_code == 503

    def test_un_mauvais_mot_de_passe_ne_pose_aucun_cookie(self, client):
        r = client.post("/login", data={"password": MDP + "x"})
        assert r.status_code == 303
        assert r.headers["Location"] == "/login?e=1"
        assert not _cookie(r)

    def test_un_prefixe_correct_ne_vaut_rien(self, client):
        r = client.post("/login", data={"password": MDP[:-1]})
        assert not _cookie(r)

    def test_un_mot_de_passe_accentue_ne_fait_pas_planter(self, client, monkeypatch):
        """`hmac.compare_digest` lève sur du non-ASCII : un « é » rendait un
        500. Refusé proprement, et accepté quand c'est le bon."""
        assert client.post("/login", data={"password": "éèà" * 5}).status_code == 303
        monkeypatch.setenv(PASSWORD_ENV, "clé-très-secrète")
        r = client.post("/login", data={"password": "clé-très-secrète"})
        assert r.headers["Location"] == "/" and _cookie(r)

    def test_les_refus_sont_indiscernables(self, client, monkeypatch):
        mauvais = client.post("/login", data={"password": "faux"})
        monkeypatch.delenv(PASSWORD_ENV, raising=False)
        absent = client.post("/login", data={"password": "faux"})
        assert (mauvais.status_code, mauvais.headers["Location"]) == \
               (absent.status_code, absent.headers["Location"])

    def test_le_mot_de_passe_saisi_nest_jamais_journalise(self, client, caplog):
        with caplog.at_level("DEBUG"):
            client.post("/login", data={"password": "tentative-secrete-123"})
        assert "tentative-secrete-123" not in caplog.text
        assert MDP not in caplog.text

    def test_le_cookie_est_protege(self, client):
        c = _cookie(client.post("/login", data={"password": MDP},
                                base_url="https://predator.example"))
        assert "HttpOnly" in c, "lisible par le JavaScript de la page"
        assert "SameSite=Lax" in c, "un site tiers pourrait faire POSTer /api/scan"
        assert "Secure" in c, "le cookie partirait en clair sur HTTP"
        assert "Max-Age=" in c

    def test_secure_ne_depend_pas_du_schema_vu_par_la_fonction(self, client):
        """Derrière le proxy de Vercel la fonction voit du HTTP : dériver
        `Secure` de `request.is_secure` l'aurait retiré en production. Seul
        le poste de dev (127.0.0.1 / localhost) en est dispensé, sinon le
        smoke test local ne garderait pas sa session."""
        prod = _cookie(client.post("/login", data={"password": MDP},
                                   base_url="http://predator.example"))
        dev = _cookie(client.post("/login", data={"password": MDP},
                                  base_url="http://127.0.0.1:5099"))
        assert "Secure" in prod
        assert dev and "Secure" not in dev

    def test_la_deconnexion_referme(self, client):
        _entrer(client)
        assert client.get("/logout").headers["Location"] == "/login"
        assert client.get("/api/signals").status_code == 401


class TestSession:
    def test_un_cookie_forge_est_refuse(self, client):
        client.set_cookie(_SESSION_COOKIE, "9999999999." + "0" * 64)
        assert client.get("/api/signals").status_code == 401

    def test_lexpiration_est_dans_la_partie_signee(self, client):
        """Repousser l'échéance à la main doit invalider le sceau."""
        expire = 2_000_000_000
        sceau = _signer_session(MDP, expire).split(".")[1]
        client.set_cookie(_SESSION_COOKIE, f"{expire + 1}.{sceau}")
        assert client.get("/api/signals").status_code == 401

    def test_un_cookie_expire_est_refuse(self, client):
        client.set_cookie(_SESSION_COOKIE, _signer_session(MDP, 1_000_000_000))
        assert client.get("/api/signals").status_code == 401

    @pytest.mark.parametrize("valeur", ["", "abc", ".", "12.", "١٢٣.x", "é.é", "1e9.x"])
    def test_un_cookie_malforme_ne_fait_pas_planter(self, client, valeur):
        client.set_cookie(_SESSION_COOKIE, valeur)
        assert client.get("/api/signals").status_code == 401

    def test_changer_le_mot_de_passe_ferme_les_sessions_ouvertes(self, client,
                                                                 monkeypatch):
        _entrer(client)
        monkeypatch.setenv(PASSWORD_ENV, "un-autre-mot-de-passe")
        assert client.get("/api/signals").status_code == 401


class TestLimiteDeDebit:
    def test_au_dela_du_quota_meme_le_bon_mot_de_passe_est_refuse(self, client):
        for _ in range(dashboard._LOGIN_LIMIT_N):
            client.post("/login", data={"password": "faux"})
        r = client.post("/login", data={"password": MDP})
        assert r.headers["Location"] == "/login?e=1" and not _cookie(r)

    def test_la_limite_de_connexion_nentame_pas_celle_du_scan(self, client):
        """Deux dictionnaires : marteler /login ne doit pas bloquer le bouton
        « Scanner » de la même IP, ni l'inverse."""
        for _ in range(dashboard._LOGIN_LIMIT_N + 1):
            client.post("/login", data={"password": "faux"})
        assert dashboard._scan_rate_limited("127.0.0.1") is False


class TestJetonDAdmin:
    def test_le_jeton_dadmin_ouvre_la_porte(self, client, monkeypatch):
        monkeypatch.setenv(ADMIN_TOKEN_ENV, "jeton-de-test-0123456789")
        r = client.get("/api/signals",
                       headers={"X-Predator-Token": "jeton-de-test-0123456789"})
        assert r.status_code == 503      # porte franchie, pas de base

    def test_une_session_nest_pas_ladministration(self, client, monkeypatch):
        """Le mot de passe du dashboard ne doit JAMAIS suffire à déclencher
        `audit.yml` : deux secrets, deux pouvoirs."""
        monkeypatch.setenv(ADMIN_TOKEN_ENV, "jeton-de-test-0123456789")
        monkeypatch.setenv("GITHUB_PAT", "pat-bidon")
        _entrer(client)
        assert client.post("/api/audit/run").status_code == 401


class TestEnTetesDeSecurite:
    @pytest.mark.parametrize("chemin", ["/api/health", "/", "/api/signals",
                                        "/static/css/predator.css"])
    def test_chaque_reponse_les_porte(self, client, chemin):
        h = client.get(chemin).headers
        assert h["X-Frame-Options"] == "DENY"
        assert h["X-Content-Type-Options"] == "nosniff"
        assert h["Referrer-Policy"] == "no-referrer"
        assert "noindex" in h["X-Robots-Tag"]


def test_la_page_de_connexion_ne_contacte_aucun_tiers():
    """Servie AVANT toute session : ni police distante, ni script, ni rien
    qui signale à un tiers qu'on se connecte ici. Lu sur la SOURCE."""
    import pathlib
    import re
    src = (pathlib.Path(__file__).resolve().parent.parent
           / "templates" / "login.html").read_text(encoding="utf-8")
    src = re.sub(r"<!--.*?-->", "", src, flags=re.S)
    assert re.search(r"(src|href|action)=\"(https?:)?//", src) is None
    assert "<script" not in src
    assert 'method="post"' in src and 'type="password"' in src
