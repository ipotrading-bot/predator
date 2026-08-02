"""
Tests de core/secret_store.py — la résolution des clés rotatives.

Ce qui est vérifié ici est exactement ce qui a coûté des heures de pipeline
aveugle : la priorité Supabase > env (une variable Vercel périmée ne doit
plus écraser une clé fraîche), et surtout le fait qu'une base injoignable ou
une table absente ne fait JAMAIS tomber la résolution — elle retombe sur
l'environnement.
"""
import pytest

from core import secret_store
from core.db import MissingCredentialsError


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def select(self, *_a, **_k):
        return self

    def eq(self, *_a, **_k):
        return self

    def limit(self, *_a, **_k):
        return self

    def execute(self):
        return type("Res", (), {"data": self._rows})()


class _FakeClient:
    def __init__(self, rows):
        self._rows = rows

    def table(self, _name):
        return _FakeQuery(self._rows)


@pytest.fixture(autouse=True)
def _clean_cache():
    secret_store.invalidate()
    secret_store._logged_source.clear()
    yield
    secret_store.invalidate()
    secret_store._logged_source.clear()


def test_supabase_wins_over_env(monkeypatch):
    """Le cas qui motive tout le module : l'env porte l'ancienne clé morte."""
    monkeypatch.setenv("ODDS_API_KEY", "ancienne_cle_morte")
    monkeypatch.setattr(secret_store, "get_db",
                        lambda write=False: _FakeClient([{"value": "nouvelle_cle"}]))
    assert secret_store.get_secret("ODDS_API_KEY") == "nouvelle_cle"


def test_env_fallback_when_table_empty(monkeypatch):
    monkeypatch.setenv("ODDS_API_KEY", "cle_env")
    monkeypatch.setattr(secret_store, "get_db",
                        lambda write=False: _FakeClient([]))
    assert secret_store.get_secret("ODDS_API_KEY") == "cle_env"


def test_env_fallback_when_table_missing(monkeypatch):
    """Migration v10_1 pas encore appliquée — doit dégrader, pas exploser."""
    class _Boom(_FakeClient):
        def table(self, _name):
            raise RuntimeError('relation "app_secrets" does not exist')

    monkeypatch.setenv("ODDS_API_KEY", "cle_env")
    monkeypatch.setattr(secret_store, "get_db", lambda write=False: _Boom([]))
    assert secret_store.get_secret("ODDS_API_KEY") == "cle_env"


def test_env_fallback_without_service_role(monkeypatch):
    """Déploiement sans SUPABASE_SERVICE_KEY (cas Vercel possible)."""
    def _raise(write=False):
        raise MissingCredentialsError("SUPABASE_SERVICE_KEY is not set")

    monkeypatch.setenv("ODDS_API_KEY", "cle_env")
    monkeypatch.setattr(secret_store, "get_db", _raise)
    assert secret_store.get_secret("ODDS_API_KEY") == "cle_env"


def test_no_db_at_all(monkeypatch):
    monkeypatch.setenv("ODDS_API_KEY", "cle_env")
    monkeypatch.setattr(secret_store, "get_db", lambda write=False: None)
    assert secret_store.get_secret("ODDS_API_KEY") == "cle_env"


def test_returns_none_when_nowhere(monkeypatch):
    monkeypatch.delenv("ODDS_API_KEY", raising=False)
    monkeypatch.setattr(secret_store, "get_db", lambda write=False: None)
    assert secret_store.get_secret("ODDS_API_KEY") is None


def test_blank_value_is_not_a_key(monkeypatch):
    """Une ligne vidée dans app_secrets ne doit pas masquer l'env."""
    monkeypatch.setenv("ODDS_API_KEY", "cle_env")
    monkeypatch.setattr(secret_store, "get_db",
                        lambda write=False: _FakeClient([{"value": "   "}]))
    assert secret_store.get_secret("ODDS_API_KEY") == "cle_env"


def test_cache_avoids_refetch(monkeypatch):
    calls = []

    def _counting(write=False):
        calls.append(1)
        return _FakeClient([{"value": "k1"}])

    monkeypatch.setattr(secret_store, "get_db", _counting)
    assert secret_store.get_secret("ODDS_API_KEY") == "k1"
    assert secret_store.get_secret("ODDS_API_KEY") == "k1"
    assert len(calls) == 1


def test_force_refresh_sees_rotation(monkeypatch):
    """Après un 401, l'appelant doit pouvoir constater une rotation."""
    monkeypatch.setattr(secret_store, "get_db",
                        lambda write=False: _FakeClient([{"value": "k1"}]))
    assert secret_store.get_secret("ODDS_API_KEY") == "k1"
    monkeypatch.setattr(secret_store, "get_db",
                        lambda write=False: _FakeClient([{"value": "k2"}]))
    assert secret_store.get_secret("ODDS_API_KEY") == "k1"          # cache
    assert secret_store.get_secret("ODDS_API_KEY", force_refresh=True) == "k2"


def test_none_is_never_cached(monkeypatch):
    """Une panne transitoire ne doit pas geler 5 min d'absence de clé."""
    monkeypatch.delenv("ODDS_API_KEY", raising=False)
    monkeypatch.setattr(secret_store, "get_db", lambda write=False: None)
    assert secret_store.get_secret("ODDS_API_KEY") is None
    monkeypatch.setattr(secret_store, "get_db",
                        lambda write=False: _FakeClient([{"value": "revenue"}]))
    assert secret_store.get_secret("ODDS_API_KEY") == "revenue"
