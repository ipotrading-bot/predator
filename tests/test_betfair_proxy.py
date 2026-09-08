"""
tests/test_betfair_proxy.py — Betfair sort par le proxy de core/net.py.

Depuis les runners GitHub (IP américaines), `certlogin` rend
`BETTING_RESTRICTED_LOCATION` à chaque scan depuis le 2026-07-09 : Betfair
géolocalise l'appelant, et aucune correction de code ne lève ça. Le
2026-09-08 l'opérateur a décidé de faire sortir Betfair par le proxy à IP
britannique déjà posé pour odds500 (`FREE_SOURCES_PROXY`, `core/net.py`).

Ce que ces tests gardent :
  1. sans proxy configuré, `requests` part en DIRECT (`proxies=None`) —
     rien ne change pour qui n'a pas de proxy ;
  2. avec un proxy, le login cert ET les appels API le reçoivent — un seul
     des deux routé, et le login passe mais l'API rend
     `BETTING_RESTRICTED_LOCATION` en silence ;
  3. le pool `closing` de `scripts/ci_env.py` transmet le proxy — la closing
     line est l'appelant le plus fréquent de Betfair, et une capacité non
     câblée meurt sans erreur ;
  4. règle 13 : le critère de retrait daté est écrit dans `core/harvester.py`
     tant que Betfair est appelé par le moteur.

Aucun réseau (tests/conftest.py) : `requests.post` est stubbé.
"""
import importlib.util
import pathlib

import pytest

from core import harvester, net


@pytest.fixture(autouse=True)
def _memo_neuf(monkeypatch):
    """`proxy_for` mémorise sa résolution pour tout le processus ; chaque
    test repart d'un mémo vide et d'une session Betfair fermée."""
    net.reset()
    harvester._betfair_session.clear()
    monkeypatch.delenv("BETFAIR_PROXY", raising=False)
    monkeypatch.delenv("FREE_SOURCES_PROXY", raising=False)
    # secret_store lirait Supabase : on force la lecture directe de l'env.
    monkeypatch.setattr(net, "proxy_for", _proxy_for_env)
    yield
    net.reset()
    harvester._betfair_session.clear()


def _proxy_for_env(source: str) -> str:
    import os
    return (os.environ.get(f"{source.upper()}_PROXY")
            or os.environ.get("FREE_SOURCES_PROXY") or "").strip()


class _Reponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload


def _stub_post(monkeypatch, payload):
    appels: list[dict] = []

    def post(url, **kw):
        appels.append({"url": url, **kw})
        return _Reponse(payload)

    monkeypatch.setattr(harvester.requests, "post", post)
    return appels


def _identifiants(monkeypatch):
    for k in ("BETFAIR_USERNAME", "BETFAIR_PASSWORD", "BETFAIR_APP_KEY",
              "BETFAIR_CERT", "BETFAIR_CERT_KEY"):
        monkeypatch.setenv(k, f"x-{k.lower()}")


def test_sans_proxy_le_login_part_en_direct(monkeypatch):
    _identifiants(monkeypatch)
    appels = _stub_post(monkeypatch, {"loginStatus": "SUCCESS", "sessionToken": "t"})
    assert harvester._betfair_login() is True
    assert len(appels) == 1
    assert appels[0]["url"] == harvester._BETFAIR_CERTLOGIN_URL
    assert appels[0]["proxies"] is None


def test_avec_proxy_le_login_cert_et_les_appels_api_le_recoivent(monkeypatch):
    _identifiants(monkeypatch)
    monkeypatch.setenv("FREE_SOURCES_PROXY", "http://u:p@lhr.example:8080")
    appels = _stub_post(monkeypatch, {"loginStatus": "SUCCESS", "sessionToken": "t"})
    assert harvester._betfair_login() is True
    _stub_post(monkeypatch, [])  # nouvel enregistreur pour l'appel API
    appels_api = _stub_post(monkeypatch, [{"marketId": "1"}])
    harvester._bf_request("listMarketCatalogue", {"filter": {}})
    attendu = {"http": "http://u:p@lhr.example:8080",
               "https": "http://u:p@lhr.example:8080"}
    assert appels[0]["proxies"] == attendu, "le login cert doit sortir par le proxy"
    assert appels_api[0]["proxies"] == attendu, "l'API doit sortir par le MÊME proxy"
    assert appels_api[0]["url"].startswith(harvester._BETFAIR_API_URL)


def test_l_override_par_source_gagne(monkeypatch):
    _identifiants(monkeypatch)
    monkeypatch.setenv("FREE_SOURCES_PROXY", "http://global:1")
    monkeypatch.setenv("BETFAIR_PROXY", "http://betfair:1")
    appels = _stub_post(monkeypatch, {"loginStatus": "SUCCESS", "sessionToken": "t"})
    harvester._betfair_login()
    assert appels[0]["proxies"]["https"] == "http://betfair:1"


def test_le_pool_closing_transmet_le_proxy():
    """La closing line appelle Betfair 3 fois par heure : sans le proxy dans
    son pool, le scan sortirait par Londres et la closing line par les
    États-Unis — et le second échouerait sans que rien ne le dise."""
    chemin = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "ci_env.py"
    spec = importlib.util.spec_from_file_location("ci_env", chemin)
    ci_env = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ci_env)
    for pool in ("scan", "closing"):
        assert "FREE_SOURCES_PROXY" in ci_env.POOLS[pool]["passthrough"], pool
        assert "BETFAIR_APP_KEY" in ci_env.POOLS[pool]["passthrough"], pool


def test_le_critere_de_retrait_est_ecrit_tant_que_betfair_est_appele():
    """AUDIT.md §3bis : critère de retrait DATÉ dans le module, retiré dans
    le même commit que la source. Betfair est « au registre » tant que le
    moteur et la closing line l'appellent."""
    src = pathlib.Path(harvester.__file__).read_text(encoding="utf-8")
    racine = pathlib.Path(harvester.__file__).resolve().parents[1]
    appelants = [racine / "run_engine.py", racine / "core" / "audit_engine.py"]
    appele = any("fetch_betfair_prices(" in p.read_text(encoding="utf-8")
                 for p in appelants)
    assert appele, "Betfair retiré des appelants : retirer aussi ce test et le critère"
    assert "CRITÈRE DE RETRAIT (règle 13, posé le 2026-09-08)" in src
