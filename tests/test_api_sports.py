"""
tests/test_api_sports.py — core/api_sports.py (famille api-sports.io).

Contexte : après l'incident 10→20 août 2026 (dix jours sans signal, toutes
les sources filtrées par IP), cette famille devient le repli principal parce
qu'elle authentifie par CLÉ. Ce que ces tests verrouillent :

- le COÛT : 1 requête calendrier + ≤ MAX_ODDS_PAGES requêtes cotes PAR DATE
  (jamais une requête par match — c'est ce qui tuait le quota) ;
- le prix SHARP (Pinnacle/exchange) est extrait comme `odds_pinnacle` et
  n'entre pas dans le line shopping soft ;
- le nul n'existe que pour le foot (X=0 ailleurs, jamais une cote inventée) ;
- les formes de réponse divergentes (fixture.id vs game.id, timestamp vs
  date ISO) sont toutes acceptées ;
- un 200 porteur d'un objet `errors` (clé non abonnée à ce sport) est un
  ÉCHEC, pas un succès vide ;
- chaque échec est loggé — le silence est ce qui a caché la panne dix jours ;
- sans clé : aucun appel réseau.
"""
import logging
from datetime import datetime, timedelta, timezone

import pytest

import core.api_sports as aps


class _Resp:
    def __init__(self, payload, status=200, remaining="50"):
        self._payload, self.status_code = payload, status
        self.headers = {"x-ratelimit-requests-remaining": remaining}

    def json(self):
        return self._payload


def _when(hours=5):
    return datetime.now(timezone.utc) + timedelta(hours=hours)


def _game(gid, home, away, *, style="fixture", hours=5):
    """style='fixture' (foot) ou 'game' (basket/baseball/hockey)."""
    dt = _when(hours)
    if style == "fixture":
        return {"fixture": {"id": gid, "timestamp": int(dt.timestamp())},
                "teams": {"home": {"name": home}, "away": {"name": away}},
                "league": {"name": "Ligue Test"}}
    return {"id": gid, "date": dt.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
            "teams": {"home": {"name": home}, "away": {"name": away}},
            "league": {"name": "Ligue Test"}}


def _bk(name, o1, o2, ox=None, bet="Match Winner"):
    values = [{"value": "Home", "odd": str(o1)}, {"value": "Away", "odd": str(o2)}]
    if ox is not None:
        values.insert(1, {"value": "Draw", "odd": str(ox)})
    return {"name": name, "bets": [{"name": bet, "values": values}]}


def _odds_row(gid, bookmakers, *, style="fixture"):
    key = "fixture" if style == "fixture" else "game"
    return {key: {"id": gid}, "bookmakers": bookmakers}


def _wire(monkeypatch, schedule, odds_pages, *, sched_status=200, odds_status=200,
          remaining="50", sched_errors=None):
    calls = {"schedule": 0, "odds": []}

    def fake_get(url, headers=None, params=None, timeout=None):
        if url.endswith("/odds"):
            calls["odds"].append((params["date"], params["page"]))
            if odds_status != 200:
                return _Resp({}, status=odds_status, remaining=remaining)
            day, page = params["date"], params["page"]
            items = odds_pages.get((day, page), [])
            total = max((p for (d, p) in odds_pages if d == day), default=1)
            return _Resp({"response": items, "paging": {"current": page, "total": total}},
                         remaining=remaining)
        calls["schedule"] += 1
        return _Resp({"response": schedule, "errors": sched_errors or []},
                     status=sched_status, remaining=remaining)

    monkeypatch.setattr(aps.requests, "get", fake_get)
    return calls


@pytest.fixture(autouse=True)
def _no_secret_lookup(monkeypatch):
    """Aucun test ne doit toucher Supabase/l'env réels."""
    monkeypatch.setattr(aps, "get_secret", lambda name, **kw: None)


def test_no_key_means_no_network(monkeypatch):
    touched = []
    monkeypatch.setattr(aps.requests, "get", lambda *a, **k: touched.append(a))
    assert aps.fetch_sport("soccer") == []
    assert aps.fetch_all() == []
    assert touched == []


def test_unknown_sport_is_ignored():
    assert aps.fetch_sport("quidditch", api_key="k") == []


def test_cost_is_one_schedule_call_plus_paged_odds(monkeypatch):
    day = _when().strftime("%Y-%m-%d")
    sched = [_game(1, "PSG", "Lyon"), _game(2, "Nice", "Lens")]
    pages = {(day, 1): [_odds_row(1, [_bk("Bwin", 1.80, 4.40, 3.60)])],
             (day, 2): [_odds_row(2, [_bk("Bet365", 2.10, 3.50, 3.30)])]}
    calls = _wire(monkeypatch, sched, pages)
    out = aps.fetch_sport("soccer", api_key="k")
    assert calls["schedule"] == 1
    assert calls["odds"] == [(day, 1), (day, 2)]
    assert sorted(m["match"] for m in out) == ["Nice vs Lens", "PSG vs Lyon"]
    assert all(m["commence_time"].endswith("Z") for m in out)
    assert all(m["sport"] == "soccer" and m["sport_id"] == 1 for m in out)


def test_page_budget_is_capped(monkeypatch):
    day = _when().strftime("%Y-%m-%d")
    sched = [_game(i, f"H{i}", f"A{i}") for i in range(1, 40)]
    pages = {(day, p): [_odds_row(p, [_bk("Bwin", 2.0, 3.5)])] for p in range(1, 12)}
    calls = _wire(monkeypatch, sched, pages)
    aps.fetch_sport("soccer", api_key="k")
    assert len(calls["odds"]) == aps.MAX_ODDS_PAGES


def test_sharp_price_is_split_out_and_excluded_from_soft(monkeypatch):
    day = _when().strftime("%Y-%m-%d")
    sched = [_game(7, "Lille", "Reims")]
    pages = {(day, 1): [_odds_row(7, [
        _bk("Pinnacle", 2.50, 2.90, 3.20),     # sharp — ne doit pas gonfler le soft
        _bk("Bwin",     2.30, 2.80, 3.40),
        _bk("Unibet",   2.20, 3.00, 3.50),
    ])]}
    _wire(monkeypatch, sched, pages)
    (m,) = aps.fetch_sport("soccer", api_key="k")
    assert m["odds_pinnacle"] == {"1": 2.50, "X": 3.20, "2": 2.90}
    assert m["odds_1xbet"] == {"1": 2.30, "X": 3.50, "2": 3.00}


def test_sharp_only_match_is_kept_but_yields_no_edge(monkeypatch):
    """Sans book soft, le prix sharp sert aussi de soft : edge nul par
    construction (jamais de faux signal), mais le match reste prixé."""
    day = _when().strftime("%Y-%m-%d")
    _wire(monkeypatch, [_game(3, "A", "B")],
          {(day, 1): [_odds_row(3, [_bk("Pinnacle", 1.95, 1.95)])]})
    (m,) = aps.fetch_sport("soccer", api_key="k")
    assert m["odds_1xbet"] == m["odds_pinnacle"]


@pytest.mark.parametrize("sport,style,sport_id", [
    ("basketball", "game", 4), ("baseball", "game", 6), ("hockey", "game", 7),
])
def test_non_soccer_uses_game_shape_and_has_no_draw(monkeypatch, sport, style, sport_id):
    day = _when().strftime("%Y-%m-%d")
    sched = [_game(11, "Home", "Away", style=style)]
    pages = {(day, 1): [_odds_row(11, [_bk("Bwin", 1.60, 2.40, 12.0)], style=style)]}
    _wire(monkeypatch, sched, pages)
    (m,) = aps.fetch_sport(sport, api_key="k")
    assert m["sport"] == sport and m["sport_id"] == sport_id
    assert m["odds_1xbet"]["X"] == 0.0        # jamais de nul hors foot
    assert m["odds_1xbet"]["1"] == 1.60 and m["odds_1xbet"]["2"] == 2.40


def test_matches_outside_the_window_are_dropped(monkeypatch):
    sched = [_game(1, "Passé", "Hier", hours=-3), _game(2, "Trop", "Loin", hours=100),
             _game(3, "Bon", "Match", hours=5)]
    day = _when().strftime("%Y-%m-%d")
    _wire(monkeypatch, sched, {(day, 1): [_odds_row(3, [_bk("Bwin", 2.0, 2.0)])]})
    out = aps.fetch_sport("soccer", api_key="k", hours_ahead=24)
    assert [m["match"] for m in out] == ["Bon vs Match"]


def test_applicative_error_on_http_200_is_a_failure(monkeypatch, caplog):
    """api-sports répond 200 + {"errors": …} quand la clé n'est pas abonnée
    à CE sport — le prendre pour un succès vide masquerait la panne."""
    calls = _wire(monkeypatch, [], {}, sched_errors={"token": "not subscribed"})
    with caplog.at_level(logging.WARNING, logger="PREDATOR.api_sports"):
        assert aps.fetch_sport("baseball", api_key="k") == []
    assert calls["odds"] == []
    assert any("refus applicatif" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("status,needle", [(401, "auth"), (429, "429"), (500, "HTTP 500")])
def test_schedule_http_errors_are_logged(monkeypatch, caplog, status, needle):
    _wire(monkeypatch, [], {}, sched_status=status)
    with caplog.at_level(logging.WARNING, logger="PREDATOR.api_sports"):
        assert aps.fetch_sport("soccer", api_key="k") == []
    assert any(needle.lower() in r.getMessage().lower() for r in caplog.records)


def test_odds_http_error_stops_cleanly_and_is_logged(monkeypatch, caplog):
    calls = _wire(monkeypatch, [_game(1, "A", "B")], {}, odds_status=429)
    with caplog.at_level(logging.WARNING, logger="PREDATOR.api_sports"):
        assert aps.fetch_sport("soccer", api_key="k") == []
    assert len(calls["odds"]) == 1            # une erreur = arrêt, pas de boucle
    assert any("HTTP 429" in r.getMessage() for r in caplog.records)


def test_quota_guard_skips_paid_calls(monkeypatch, caplog):
    calls = _wire(monkeypatch, [_game(1, "A", "B")], {}, remaining="3")
    with caplog.at_level(logging.INFO, logger="PREDATOR.api_sports"):
        assert aps.fetch_sport("soccer", api_key="k") == []
    assert calls["odds"] == []
    assert any("garde quota" in r.getMessage() for r in caplog.records)


def test_fetch_all_isolates_a_failing_sport(monkeypatch):
    """Un sport qui explose ne doit pas emporter les autres."""
    def flaky(sport, hours_ahead=24):
        if sport == "basketball":
            raise RuntimeError("boom")
        return [{"match": f"{sport} match"}]

    monkeypatch.setattr(aps, "fetch_sport", flaky)
    out = aps.fetch_all()
    assert {m["match"] for m in out} == {"soccer match", "baseball match", "hockey match"}


def test_available_sports_reports_configured_keys(monkeypatch):
    monkeypatch.setattr(aps, "get_secret",
                        lambda name, **kw: "k" if name in ("API_FOOTBALL_KEY", "API_SPORTS_KEY") else None)
    # API_SPORTS_KEY est la clé commune : elle débloque les quatre sports.
    assert aps.available_sports() == ["soccer", "basketball", "baseball", "hockey"]

    monkeypatch.setattr(aps, "get_secret",
                        lambda name, **kw: "k" if name == "API_FOOTBALL_KEY" else None)
    assert aps.available_sports() == ["soccer"]
