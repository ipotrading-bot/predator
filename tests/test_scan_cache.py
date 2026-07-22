"""
tests/test_scan_cache.py — run_engine._get_cached / _set_cached.

Régression 2026-07-22 : un résultat de recherche web VIDE n'était jamais
mis en cache (`if mma_events and sb: _set_cached(...)`), donc chacun des
~62 runs quotidiens (48 Golden Hour + 10 engine + 4 deep scan) relançait la
recherche MMA/eSports/alt sports. À ~3,5k tokens l'appel compound-mini, ça
vidait les 100 000 TPD de llama-3.3-70b-versatile avant que l'audit des 6h
puisse settler quoi que ce soit → ai_learning_ledger figé → /performance
figé (runs audit 29886717393 / 29904315408 / 29926411707 : "0 settled").
"""
import json
from datetime import datetime, timedelta, timezone

import run_engine


class _FakeTable:
    def __init__(self, store):
        self._store = store
        self._key = None

    def select(self, *_a, **_kw):
        return self

    def eq(self, _col, value):
        self._key = value
        return self

    def maybe_single(self):
        return self

    def execute(self):
        return type("R", (), {"data": self._store.get(self._key)})()


class _FakeSb:
    def __init__(self, store):
        self._store = store

    def table(self, _name):
        return _FakeTable(self._store)


def _row(value, age_h):
    return {
        "value": json.dumps(value),
        "updated_at": (datetime.now(timezone.utc) - timedelta(hours=age_h)).isoformat(),
    }


def test_empty_cache_entry_is_a_hit_while_fresh():
    """Un [] récent doit court-circuiter la recherche web, pas la relancer."""
    sb = _FakeSb({"cache_mma": _row([], age_h=0.5)})
    assert run_engine._get_cached(sb, "cache_mma", 8) == []


def test_empty_cache_entry_expires_faster_than_a_populated_one():
    """Après le TTL court, on retente — un [] ne doit pas geler 8h."""
    store = {"cache_mma": _row([], age_h=4)}
    assert run_engine._get_cached(_FakeSb(store), "cache_mma", 8) is None

    # ... alors qu'un cache non vide du même âge reste valide jusqu'à 8h.
    store["cache_mma"] = _row([{"match": "A vs B"}], age_h=4)
    assert run_engine._get_cached(_FakeSb(store), "cache_mma", 8) == [{"match": "A vs B"}]


def test_populated_cache_expires_at_its_own_ttl():
    sb = _FakeSb({"cache_mma": _row([{"match": "A vs B"}], age_h=9)})
    assert run_engine._get_cached(sb, "cache_mma", 8) is None
