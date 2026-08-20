"""
tests/test_api_football.py — core/api_football.py v10.3.

Ce que ces tests verrouillent :
- le coût d'un cycle est 1 /fixtures + ≤ MAX_ODDS_PAGES /odds par date
  (plus jamais un /odds par fixture) ;
- Pinnacle présent dans la réponse → `odds_pinnacle` sur le match, et le
  meilleur prix SOFT exclut Pinnacle ;
- `commence_time` est posé (les matchs Tier 2 sans heure tombaient dans un
  seau « inconnu » en aval) ;
- un /odds ≠ 200 arrête proprement ET se voit dans les logs — plus jamais
  de [] silencieux ;
- sans clé → [] sans appel réseau.
"""
import logging
from datetime import datetime, timedelta, timezone

import core.api_football as af


class _Resp:
    def __init__(self, payload, status=200, remaining="50"):
        self._payload = payload
        self.status_code = status
        self.headers = {"x-ratelimit-requests-remaining": remaining}

    def json(self):
        return self._payload


def _fixture(fid, home, away, hours_from_now=5, league="Ligue 1"):
    ts = int((datetime.now(timezone.utc) + timedelta(hours=hours_from_now)).timestamp())
    return {"fixture": {"id": fid, "timestamp": ts},
            "teams": {"home": {"name": home}, "away": {"name": away}},
            "league": {"name": league}}


def _bk(name, o1, ox, o2, bet_name="Match Winner"):
    return {"name": name, "bets": [{"name": bet_name, "values": [
        {"value": "Home", "odd": str(o1)}, {"value": "Draw", "odd": str(ox)},
        {"value": "Away", "odd": str(o2)}]}]}


def _odds_item(fid, bookmakers):
    return {"fixture": {"id": fid}, "bookmakers": bookmakers}


def _wire(monkeypatch, fixtures, odds_pages, odds_status=200, remaining="50"):
    """odds_pages : {(date, page): [items]} — toute page absente = réponse vide."""
    calls = {"fixtures": 0, "odds": []}

    def fake_get(url, headers=None, params=None, timeout=None):
        if url.endswith("/fixtures"):
            calls["fixtures"] += 1
            return _Resp({"response": fixtures}, remaining=remaining)
        assert url.endswith("/odds")
        key = (params["date"], params["page"])
        calls["odds"].append(key)
        if odds_status != 200:
            return _Resp({}, status=odds_status, remaining=remaining)
        items = odds_pages.get(key, [])
        total = max((p for (d, p) in odds_pages if d == params["date"]), default=1)
        return _Resp({"response": items, "paging": {"current": params["page"], "total": total}},
                     remaining=remaining)

    monkeypatch.setattr(af.requests, "get", fake_get)
    return calls


def test_no_key_means_no_network(monkeypatch):
    monkeypatch.setattr(af, "get_secret", lambda name, **kw: None)
    called = []
    monkeypatch.setattr(af.requests, "get", lambda *a, **k: called.append(a))
    assert af.fetch_football_matches() == []
    assert called == []


def test_one_fixtures_call_plus_paged_odds_by_date(monkeypatch):
    fx = [_fixture(1, "PSG", "Lyon"), _fixture(2, "Nice", "Lens")]
    day = (datetime.now(timezone.utc) + timedelta(hours=5)).strftime("%Y-%m-%d")
    pages = {(day, 1): [_odds_item(1, [_bk("Bwin", 1.80, 3.60, 4.40)])],
             (day, 2): [_odds_item(2, [_bk("Bet365", 2.10, 3.30, 3.50)])]}
    calls = _wire(monkeypatch, fx, pages)
    out = af.fetch_football_matches(api_key="k", hours_ahead=24)
    assert calls["fixtures"] == 1
    assert calls["odds"] == [(day, 1), (day, 2)]          # paginé, pas un appel par fixture
    assert sorted(m["match"] for m in out) == ["Nice vs Lens", "PSG vs Lyon"]
    assert all(m["commence_time"].endswith("Z") for m in out)
    assert all("odds_pinnacle" not in m for m in out)


def test_pinnacle_is_extracted_as_sharp_and_excluded_from_soft(monkeypatch):
    fx = [_fixture(7, "Lille", "Reims")]
    day = (datetime.now(timezone.utc) + timedelta(hours=5)).strftime("%Y-%m-%d")
    pages = {(day, 1): [_odds_item(7, [
        _bk("Pinnacle", 2.50, 3.20, 2.90),   # sharp — ne doit pas gonfler le soft
        _bk("Bwin",     2.30, 3.40, 2.80),
        _bk("Unibet",   2.20, 3.50, 3.00),
    ])]}
    _wire(monkeypatch, fx, pages)
    (m,) = af.fetch_football_matches(api_key="k")
    assert m["odds_pinnacle"] == {"1": 2.50, "X": 3.20, "2": 2.90}
    assert m["odds_1xbet"] == {"1": 2.30, "X": 3.50, "2": 3.00}   # best-of soft, sans Pinnacle


def test_odds_http_error_stops_cleanly_and_is_logged(monkeypatch, caplog):
    fx = [_fixture(1, "A", "B")]
    calls = _wire(monkeypatch, fx, {}, odds_status=429)
    with caplog.at_level(logging.WARNING, logger="PREDATOR.api_football"):
        assert af.fetch_football_matches(api_key="k") == []
    assert len(calls["odds"]) == 1                        # une erreur = on s'arrête, on ne boucle pas
    assert any("HTTP 429" in r.getMessage() for r in caplog.records)


def test_quota_guard_skips_paid_odds_when_nearly_exhausted(monkeypatch, caplog):
    fx = [_fixture(1, "A", "B")]
    calls = _wire(monkeypatch, fx, {}, remaining="3")   # < _QUOTA_GUARD_THRESHOLD
    with caplog.at_level(logging.INFO, logger="PREDATOR.api_football"):
        assert af.fetch_football_matches(api_key="k") == []
    assert calls["odds"] == []
    assert any("garde quota" in r.getMessage() for r in caplog.records)


def test_page_budget_is_capped(monkeypatch):
    fx = [_fixture(i, f"H{i}", f"A{i}") for i in range(1, 40)]
    day = (datetime.now(timezone.utc) + timedelta(hours=5)).strftime("%Y-%m-%d")
    pages = {(day, p): [_odds_item(p, [_bk("Bwin", 2.0, 3.0, 3.5)])] for p in range(1, 10)}
    calls = _wire(monkeypatch, fx, pages)
    af.fetch_football_matches(api_key="k")
    assert len(calls["odds"]) == af.MAX_ODDS_PAGES
