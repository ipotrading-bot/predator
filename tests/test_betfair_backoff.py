"""
tests/test_betfair_backoff.py — le login Betfair ne se rejoue pas après un
refus de COMPTE (2026-09-08).

Mesuré le 2026-09-08 19:30 (run closing line 34269384757) : « Betfair login:
TEMPORARY_BAN_TOO_MANY_REQUESTS ». Chaque scan (22/j) et chaque tick de
closing line (~45/j) retentait le cert-login pendant que le certificat
n'était pas associé au compte (CERT_AUTH_REQUIRED, action opérateur) : ~70
échecs par jour, ban temporaire. Ce qui est verrouillé ici :
- un refus de compte suspend les tentatives pour une durée qui dépend du
  statut, partagée entre processus (table meta) ;
- tant que la suspension court, AUCUNE requête de login ne part ;
- un incident réseau (exception) ne suspend rien : il est transitoire ;
- un SUCCESS ne suspend rien.
"""
from datetime import datetime, timedelta, timezone

import pytest

import core.harvester as hv


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def _env_et_meta(monkeypatch):
    for k in ("BETFAIR_USERNAME", "BETFAIR_PASSWORD", "BETFAIR_APP_KEY",
              "BETFAIR_CERT", "BETFAIR_CERT_KEY"):
        monkeypatch.setenv(k, "x")
    meta = {}
    monkeypatch.setattr(hv, "_meta_get", lambda key: meta.get(key))
    monkeypatch.setattr(hv, "_meta_set", lambda key, value: meta.__setitem__(key, value))
    monkeypatch.setattr(hv, "_betfair_proxies", lambda: None)
    hv._betfair_session.clear()
    yield meta
    hv._betfair_session.clear()


def _poster(monkeypatch, status):
    appels = {"n": 0}

    def fake_post(*a, **k):
        appels["n"] += 1
        if isinstance(status, Exception):
            raise status
        return _Resp({"loginStatus": status, "sessionToken": "t"})

    monkeypatch.setattr(hv.requests, "post", fake_post)
    return appels


@pytest.mark.parametrize("status, heures", [
    ("CERT_AUTH_REQUIRED", 6.0),
    ("TEMPORARY_BAN_TOO_MANY_REQUESTS", 3.0),
    ("SOME_OTHER_REFUSAL", 1.0),
])
def test_un_refus_de_compte_suspend_les_tentatives(monkeypatch, _env_et_meta, status, heures):
    appels = _poster(monkeypatch, status)
    assert hv._betfair_login() is False
    assert appels["n"] == 1
    until = datetime.fromisoformat(_env_et_meta[hv._BETFAIR_BACKOFF_KEY])
    delta = until - datetime.now(timezone.utc)
    assert timedelta(hours=heures) - timedelta(minutes=2) < delta <= timedelta(hours=heures)
    # Tant que la suspension court : zéro requête.
    assert hv._betfair_login() is False
    assert appels["n"] == 1
    assert hv.fetch_betfair_prices(sports=["soccer"]) == {}
    assert appels["n"] == 1


def test_la_suspension_expiree_laisse_retenter(monkeypatch, _env_et_meta):
    _env_et_meta[hv._BETFAIR_BACKOFF_KEY] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    appels = _poster(monkeypatch, "SUCCESS")
    assert hv._betfair_login() is True
    assert appels["n"] == 1
    assert hv._betfair_session.get("token") == "t"


def test_un_succes_ou_un_incident_reseau_ne_suspendent_rien(monkeypatch, _env_et_meta):
    _poster(monkeypatch, "SUCCESS")
    assert hv._betfair_login() is True
    assert hv._BETFAIR_BACKOFF_KEY not in _env_et_meta
    hv._betfair_session.clear()
    _poster(monkeypatch, ConnectionError("réseau"))
    assert hv._betfair_login() is False
    assert hv._BETFAIR_BACKOFF_KEY not in _env_et_meta


def test_la_suspension_est_lisible_sans_base():
    """Sans meta (tests, sandbox) : pas de suspension, jamais d'exception."""
    assert hv.betfair_login_suspendu() is None


# ── Suspension décidée par l'opérateur (2026-09-08 soir : compte bloqué) ──

def test_la_suspension_operateur_coupe_tout_sans_tentative(monkeypatch, _env_et_meta, caplog):
    """`meta.betfair_suspendu` posé : {} tout de suite, zéro requête, zéro
    backoff écrit, une ligne INFO — Matchbook/Smarkets tiennent le Tier 1.5."""
    import logging
    _env_et_meta[hv.BETFAIR_SUSPENDU_KEY] = "2026-09-08 compte bloqué (opérateur)"
    appels = _poster(monkeypatch, "SUCCESS")
    with caplog.at_level(logging.INFO, logger="PREDATOR.harvester"):
        assert hv.fetch_betfair_prices(sports=["soccer"]) == {}
    assert appels["n"] == 0
    assert hv._BETFAIR_BACKOFF_KEY not in _env_et_meta
    assert any("suspendu par l'opérateur" in r.getMessage() for r in caplog.records)
    assert hv.betfair_suspendu() == "2026-09-08 compte bloqué (opérateur)"


@pytest.mark.parametrize("valeur", ["", "0", "non", "off", "  "])
def test_une_valeur_vide_ou_non_leve_la_suspension(monkeypatch, _env_et_meta, valeur):
    _env_et_meta[hv.BETFAIR_SUSPENDU_KEY] = valeur
    assert hv.betfair_suspendu() is None
    appels = _poster(monkeypatch, "SUCCESS")
    hv.fetch_betfair_prices(sports=["soccer"])
    assert appels["n"] >= 1, "suspension levée : le login repart"
