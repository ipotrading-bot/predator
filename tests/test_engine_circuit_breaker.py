"""
tests/test_engine_circuit_breaker.py — run_engine.py : coupe-circuit du
harvest Tier 2 et alertes dédupliquées (v10.3, incident du 2026-08-10).

Contrat :
- un harvest vide pose `meta.harvest_empty_at` ; tant qu'il a moins de
  HARVEST_EMPTY_TTL_H, `_harvest_recently_empty()` rend son âge (→ on saute
  Groq/Tavily) ; un harvest non vide l'efface ;
- `_alert_once()` n'envoie qu'une fois par TTL, et envoie TOUJOURS sans
  Supabase (un doublon vaut mieux qu'un silence) ;
- `_alert_oddsapi_pool_if_dead()` ne parle que si le pool existe ET est
  entièrement mort — « pas de clé » et « pool mort » sont deux messages.
"""
from datetime import datetime, timedelta, timezone

import run_engine as eng


class _Q:
    """Chaîne table().select().eq().maybe_single().execute() / upsert().execute()."""
    def __init__(self, store, table):
        self.store, self.table_name, self._key = store, table, None

    def select(self, *_a, **_k): return self
    def eq(self, _col, val): self._key = val; return self
    def maybe_single(self): return self
    def upsert(self, row, **_k): self.store[row["key"]] = row; return self

    def execute(self):
        class R: pass
        r = R(); r.data = self.store.get(self._key); return r


class FakeSB:
    def __init__(self): self.store = {}
    def table(self, name): return _Q(self.store, name)


def _iso(hours_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


def test_empty_harvest_is_remembered_then_forgotten(monkeypatch):
    sb = FakeSB()
    assert eng._harvest_recently_empty(sb) is None
    eng._note_harvest_result(sb, [])
    age = eng._harvest_recently_empty(sb)
    assert age is not None and age < 0.1
    eng._note_harvest_result(sb, [{"match": "A vs B"}])
    assert eng._harvest_recently_empty(sb) is None


def test_old_empty_harvest_does_not_block(monkeypatch):
    sb = FakeSB()
    sb.store["harvest_empty_at"] = {"key": "harvest_empty_at",
                                    "value": _iso(eng._HARVEST_EMPTY_TTL_H + 1)}
    assert eng._harvest_recently_empty(sb) is None


def test_alert_once_dedupes_within_ttl(monkeypatch):
    sent = []
    monkeypatch.setattr(eng, "_telegram", lambda t: sent.append(t))
    sb = FakeSB()
    assert eng._alert_once(sb, "alert_x", "hello") is True
    assert eng._alert_once(sb, "alert_x", "hello") is False
    assert sent == ["hello"]
    sb.store["alert_x"]["value"] = _iso(eng._ALERT_TTL_H + 1)
    assert eng._alert_once(sb, "alert_x", "hello") is True
    assert len(sent) == 2


def test_alert_once_without_db_always_sends(monkeypatch):
    sent = []
    monkeypatch.setattr(eng, "_telegram", lambda t: sent.append(t))
    assert eng._alert_once(None, "alert_x", "a") and eng._alert_once(None, "alert_x", "b")
    assert sent == ["a", "b"]


def test_pool_dead_alert_names_the_cause(monkeypatch):
    sent = []
    monkeypatch.setattr(eng, "_telegram", lambda t: sent.append(t))
    monkeypatch.setattr(eng, "_odds_pool_status",
                        lambda: {"total": 2, "dead": 2, "live": 0, "reason": "HTTP 401"})
    eng._alert_oddsapi_pool_if_dead(FakeSB())
    assert len(sent) == 1 and "2/2" in sent[0] and "rotate_odds_key.py --add" in sent[0]

    sent.clear()
    monkeypatch.setattr(eng, "_odds_pool_status",
                        lambda: {"total": 2, "dead": 1, "live": 1, "reason": "HTTP 401"})
    eng._alert_oddsapi_pool_if_dead(FakeSB())
    assert sent == []                                   # une clé vivante = pas d'alerte

    monkeypatch.setattr(eng, "_odds_pool_status",
                        lambda: {"total": 0, "dead": 0, "live": 0, "reason": ""})
    eng._alert_oddsapi_pool_if_dead(FakeSB())
    assert len(sent) == 1 and "aucune clé" in sent[0]
