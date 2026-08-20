"""
tests/test_odds_api_keypool.py — core/odds_api.py : pool de clés + bascule.

Incident du 2026-08-10 : clé unique à 0 crédit, jamais tournée, dix jours de
« 0 matchs, 0 signaux ». Ces tests verrouillent le contrat qui empêche la
récidive : une clé morte est écartée (sonde gratuite ou 401/403/422 en cours
de scan) et le scan CONTINUE sur la suivante, même ligue, sans rien perdre ;
seul un pool entièrement mort rend [] — et se signale comme tel.
"""
import pytest

import core.odds_api as odds_api


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.headers = {"x-requests-remaining": "10", "x-requests-used": "1"}

    def json(self):
        return self._payload


def _event(eid="e1"):
    return {"id": eid, "sport_key": "basketball_nba", "commence_time": "2030-01-01T00:00:00Z",
            "home_team": "A", "away_team": "B",
            "bookmakers": [{"key": "pinnacle", "markets": [{"key": "h2h", "outcomes": [
                {"name": "A", "price": 1.9}, {"name": "B", "price": 1.9}]}]},
                {"key": "onexbet", "markets": [{"key": "h2h", "outcomes": [
                {"name": "A", "price": 2.0}, {"name": "B", "price": 1.8}]}]}]}


def _wire(monkeypatch, dead: set, odds_payload=None):
    """Stub requests.get : les clés dans `dead` répondent 401 partout."""
    calls = {"probe": [], "odds": []}

    def fake_get(url, params=None, timeout=None):
        key = (params or {}).get("apiKey")
        if url.rstrip("/").endswith("/sports"):
            calls["probe"].append(key)
            return _Resp([], status=401 if key in dead else 200)
        if "/events" in url:
            return _Resp([{"id": "x"}] if key not in dead else None,
                         status=401 if key in dead else 200)
        calls["odds"].append(key)
        if key in dead:
            return _Resp(None, status=401)
        return _Resp(odds_payload if odds_payload is not None else [_event()])

    monkeypatch.setattr(odds_api.requests, "get", fake_get)
    monkeypatch.setattr(odds_api, "get_secret", lambda name, **kw: None)
    odds_api.reset_pool()
    return calls


@pytest.fixture(autouse=True)
def _clean_pool():
    odds_api.reset_pool()
    yield
    odds_api.reset_pool()


def test_pool_is_parsed_from_all_sources(monkeypatch):
    secrets = {"ODDS_API_KEYS": "k1, k2\nk3", "ODDS_API_KEY": "k2"}
    monkeypatch.setattr(odds_api, "get_secret", lambda name, **kw: secrets.get(name))
    monkeypatch.setenv("ODDS_API_KEY_2", "k4")
    assert odds_api.candidate_keys() == ["k1", "k2", "k3", "k4"]
    assert odds_api.candidate_keys("explicit") == ["explicit", "k1", "k2", "k3", "k4"]


def test_dead_first_key_is_skipped_by_the_free_probe(monkeypatch):
    calls = _wire(monkeypatch, dead={"dead"})
    events = odds_api.fetch_odds(api_key="dead,live", hours_ahead=2,
                                 sport_keys={"basketball_nba": "basketball"})
    assert len(events) == 1
    assert calls["probe"] == ["dead", "live"]
    assert calls["odds"] == ["live"]            # la clé morte n'a jamais été facturée
    st = odds_api.pool_status("dead,live")
    assert (st["total"], st["dead"], st["live"]) == (2, 1, 1)


def test_key_dying_mid_scan_rotates_and_replays_the_same_league(monkeypatch):
    # "k1" passe la sonde puis meurt à la 2e ligue : la 2e ligue doit être
    # rejouée sur k2, pas sautée.
    calls = {"odds": []}
    state = {"k1_paid": 0}

    def fake_get(url, params=None, timeout=None):
        key = params["apiKey"]
        if url.rstrip("/").endswith("/sports"):
            return _Resp([])
        if "/events" in url:
            return _Resp([{"id": "x"}])
        league = url.split("/sports/")[1].split("/")[0]
        calls["odds"].append((key, league))
        if key == "k1":
            state["k1_paid"] += 1
            if state["k1_paid"] >= 2:
                return _Resp(None, status=422)
        return _Resp([_event(f"{key}-{league}")])

    monkeypatch.setattr(odds_api.requests, "get", fake_get)
    monkeypatch.setattr(odds_api, "get_secret", lambda name, **kw: None)
    events = odds_api.fetch_odds(api_key="k1,k2", hours_ahead=2,
                                 sport_keys={"basketball_nba": "basketball",
                                             "icehockey_nhl": "hockey"})
    leagues = [e["id"].split("-", 1)[1] for e in events]
    assert sorted(leagues) == ["basketball_nba", "icehockey_nhl"]
    # la ligue qui a vu le 422 a été rejouée sur k2
    assert ("k2", calls["odds"][-1][1]) == calls["odds"][-1]
    assert odds_api.pool_status("k1,k2")["dead"] == 1


def test_fully_dead_pool_returns_empty_and_reports_exhaustion(monkeypatch):
    calls = _wire(monkeypatch, dead={"a", "b"})
    assert odds_api.fetch_odds(api_key="a,b", hours_ahead=2,
                               sport_keys={"basketball_nba": "basketball"}) == []
    assert calls["odds"] == []                  # rien facturé sur des clés mortes
    monkeypatch.setattr(odds_api, "get_secret",
                        lambda name, **kw: "a,b" if name == odds_api.POOL_SECRET else None)
    assert odds_api.pool_exhausted() is True


def test_no_key_at_all_is_not_exhaustion(monkeypatch):
    monkeypatch.setattr(odds_api, "get_secret", lambda name, **kw: None)
    for i in range(2, 10):
        monkeypatch.delenv(f"ODDS_API_KEY_{i}", raising=False)
    assert odds_api.candidate_keys() == []
    assert odds_api.pool_exhausted() is False   # pas de clé ≠ clés mortes
