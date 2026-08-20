"""
tests/test_matchbook.py — core/matchbook.py (exchange sharp, sans clé).

Matchbook est devenu la référence sharp du pipeline : c'est elle qui donne la
probabilité vraie quand le pool OddsAPI est mort. Une erreur ici ne produit
pas une panne visible — elle produit des signaux FAUX. D'où les garde-fous
verrouillés ci-dessous :

- « Raiders at Texans » : le « at » américain inverse domicile/extérieur. Se
  tromper intervertirait les cotes 1 et 2, donc l'edge, sans rien casser.
- milieu back/lay : une fourchette absente, croisée ou trop large donne un
  prix fantaisiste (back 110 / lay 1.01 sur un carnet vide) — il ne doit
  jamais entrer dans le devig.
- somme des probabilités hors plage = carnet périmé → rejet.
- « money_line » (US/tennis) et « one_x_two » (foot) sont deux noms du même
  marché ; n'en reconnaître qu'un a fait perdre basket, baseball et tennis
  au premier essai.
- un 403/451 (géoblocage US possible sur les runners) rend {} en le disant,
  jamais une exception.
"""
import logging

import pytest

import core.matchbook as mb


class _Resp:
    def __init__(self, payload, status=200):
        self._payload, self.status_code = payload, status

    def json(self):
        if self._payload is None:
            raise ValueError("réponse illisible")
        return self._payload


def _prices(back=None, lay=None):
    out = []
    if back is not None:
        out.append({"side": "back", "odds": back})
    if lay is not None:
        out.append({"side": "lay", "odds": lay})
    return out


def _event(name, runners, *, mtype="one_x_two", start=None, live=False, status="open"):
    from datetime import datetime, timedelta, timezone
    start = start or (datetime.now(timezone.utc) + timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return {
        "id": abs(hash(name)) % 10**9, "name": name, "start": start,
        "in-running-flag": live,
        "markets": [{"market-type": mtype, "status": status,
                     "runners": [{"name": n, "prices": _prices(b, l)} for n, b, l in runners]}],
    }


def _wire(monkeypatch, events, *, status=200, total=None, capture=None):
    pages = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        if capture is not None:
            capture.append(params)
        if status != 200:
            return _Resp({}, status=status)
        off = int((params or {}).get("offset", 0))
        per = int((params or {}).get("per-page", 100))
        chunk = events[off:off + per]
        pages[off] = chunk
        return _Resp({"events": chunk, "total": total if total is not None else len(events)})

    monkeypatch.setattr(mb.requests, "get", fake_get)


# ── Le piège domicile/extérieur ───────────────────────────────────────

@pytest.mark.parametrize("name,expected", [
    ("Arsenal vs Chelsea",              ("Arsenal", "Chelsea")),
    ("Arsenal v Chelsea",               ("Arsenal", "Chelsea")),
    ("Raiders at Texans",               ("Texans", "Raiders")),      # « at » = inversion
    ("Yankees @ Orioles",               ("Orioles", "Yankees")),
    ("PSG",                             None),
    ("  vs Chelsea",                    None),
])
def test_split_teams_handles_both_conventions(name, expected):
    assert mb._split_teams(name) == expected


def test_us_at_convention_maps_odds_to_the_right_team(monkeypatch):
    """« Yankees at Orioles » : Orioles reçoivent, donc leur cote est « 1 »."""
    ev = _event("New York Yankees at Baltimore Orioles",
                [("New York Yankees", 1.96, 1.98), ("Baltimore Orioles", 2.02, 2.04)],
                mtype="money_line")
    _wire(monkeypatch, [ev])
    out = mb.fetch_matchbook_prices(sports=["baseball"])
    (row,) = out.values()
    assert row["home"] == "Baltimore Orioles" and row["away"] == "New York Yankees"
    assert row["1"] == pytest.approx(2.03)      # Orioles, à domicile
    assert row["2"] == pytest.approx(1.97)      # Yankees, à l'extérieur
    assert "baltimore orioles_new york yankees" in out


# ── Qualité du prix ───────────────────────────────────────────────────

def test_mid_is_the_back_lay_midpoint():
    assert mb._mid(_prices(2.00, 2.10)) == pytest.approx(2.05)


@pytest.mark.parametrize("back,lay", [
    (2.00, None),      # pas de lay : aucune fourchette
    (None, 2.00),      # pas de back
    (2.50, 2.00),      # carnet croisé
    (2.00, 110.0),     # fourchette absurde (carnet vide)
])
def test_unusable_book_yields_no_price(back, lay):
    assert mb._mid(_prices(back, lay)) is None


def test_thin_market_is_dropped_entirely(monkeypatch):
    ev = _event("A vs B", [("A", 110.0, None), ("B", None, 1.01), ("Draw", 75.0, None)])
    _wire(monkeypatch, [ev])
    assert mb.fetch_matchbook_prices(sports=["soccer"]) == {}


def test_stale_book_rejected_on_overround(monkeypatch):
    # Deux favoris simultanés : somme des probas ≈ 1.43, carnet incohérent.
    ev = _event("A vs B", [("A", 1.40, 1.42), ("B", 1.40, 1.42)], mtype="money_line")
    _wire(monkeypatch, [ev])
    assert mb.fetch_matchbook_prices(sports=["soccer"]) == {}


def test_soccer_keeps_the_draw(monkeypatch):
    ev = _event("Lille vs Reims",
                [("Lille", 2.30, 2.36), ("Draw", 3.40, 3.50), ("Reims", 3.10, 3.20)])
    _wire(monkeypatch, [ev])
    (row,) = mb.fetch_matchbook_prices(sports=["soccer"]).values()
    assert row["X"] == pytest.approx(3.45)
    assert 0.90 <= 1 / row["1"] + 1 / row["X"] + 1 / row["2"] <= 1.12


def test_money_line_market_is_recognised(monkeypatch):
    """Ne reconnaître que « one_x_two » perdait basket/baseball/tennis."""
    ev = _event("Fever at Wings", [("Fever", 2.60, 2.70), ("Wings", 1.55, 1.60)],
                mtype="money_line")
    _wire(monkeypatch, [ev])
    assert len(mb.fetch_matchbook_prices(sports=["basketball"])) == 1


# ── Filtres de fenêtre et d'état ──────────────────────────────────────

def test_in_running_events_are_skipped(monkeypatch):
    ev = _event("A vs B", [("A", 2.0, 2.1), ("B", 2.0, 2.1)], live=True)
    _wire(monkeypatch, [ev])
    assert mb.fetch_matchbook_prices(sports=["soccer"]) == {}


def test_events_outside_the_window_are_skipped(monkeypatch):
    from datetime import datetime, timedelta, timezone
    far = (datetime.now(timezone.utc) + timedelta(hours=100)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    past = (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    runners = [("A", 2.0, 2.1), ("B", 2.0, 2.1)]
    _wire(monkeypatch, [_event("A vs B", runners, start=far),
                        _event("C vs D", runners, start=past)])
    assert mb.fetch_matchbook_prices(sports=["soccer"], hours_ahead=24) == {}


def test_closed_market_is_skipped(monkeypatch):
    ev = _event("A vs B", [("A", 2.0, 2.1), ("B", 2.0, 2.1)], status="suspended")
    _wire(monkeypatch, [ev])
    assert mb.fetch_matchbook_prices(sports=["soccer"]) == {}


# ── Robustesse réseau ─────────────────────────────────────────────────

@pytest.mark.parametrize("status", [403, 451, 401])
def test_geoblock_returns_empty_and_says_so(monkeypatch, caplog, status):
    _wire(monkeypatch, [], status=status)
    with caplog.at_level(logging.WARNING, logger="PREDATOR.matchbook"):
        assert mb.fetch_matchbook_prices(sports=["soccer"]) == {}
    assert any("géoblocage" in r.getMessage() for r in caplog.records)


def test_network_error_is_swallowed(monkeypatch):
    def boom(*a, **k):
        raise ConnectionError("réseau")
    monkeypatch.setattr(mb.requests, "get", boom)
    assert mb.fetch_matchbook_prices(sports=["soccer"]) == {}


def test_unparseable_body_is_swallowed(monkeypatch):
    monkeypatch.setattr(mb.requests, "get", lambda *a, **k: _Resp(None))
    assert mb.fetch_matchbook_prices(sports=["soccer"]) == {}


def test_unknown_sport_makes_no_call(monkeypatch):
    touched = []
    monkeypatch.setattr(mb.requests, "get", lambda *a, **k: touched.append(a))
    assert mb.fetch_matchbook_prices(sports=["quidditch"]) == {}
    assert touched == []


def test_pagination_walks_until_total(monkeypatch):
    events = [_event(f"Home{i} vs Away{i}",
                     [(f"Home{i}", 2.0, 2.1), (f"Away{i}", 2.0, 2.1)], mtype="money_line")
              for i in range(250)]
    seen = []
    monkeypatch.setattr(mb, "PER_PAGE", 100)
    _wire(monkeypatch, events, capture=seen)
    out = mb.fetch_matchbook_prices(sports=["soccer"])
    assert [int(p["offset"]) for p in seen] == [0, 100, 200]
    assert len(out) == 250


def test_pagination_is_capped(monkeypatch):
    events = [_event(f"Home{i} vs Away{i}",
                     [(f"Home{i}", 2.0, 2.1), (f"Away{i}", 2.0, 2.1)], mtype="money_line")
              for i in range(5000)]
    seen = []
    monkeypatch.setattr(mb, "PER_PAGE", 100)
    monkeypatch.setattr(mb, "MAX_PAGES", 3)
    _wire(monkeypatch, events, capture=seen)
    mb.fetch_matchbook_prices(sports=["soccer"])
    assert len(seen) == 3


def test_probe_reports_reachability(monkeypatch):
    monkeypatch.setattr(mb.requests, "get", lambda *a, **k: _Resp({"total": 42, "events": []}))
    ok, detail = mb.probe()
    assert ok and "42" in detail

    monkeypatch.setattr(mb.requests, "get", lambda *a, **k: _Resp({}, status=451))
    ok, detail = mb.probe()
    assert not ok and "géoblocage" in detail


def test_abbreviated_runner_names_are_matched_fuzzily(monkeypatch):
    """L'exchange abrège : « Man Utd » pour « Manchester United »."""
    ev = _event("Manchester United vs Arsenal",
                [("Man Utd", 2.30, 2.36), ("Draw", 3.40, 3.50), ("Arsenal", 3.10, 3.20)])
    _wire(monkeypatch, [ev])
    out = mb.fetch_matchbook_prices(sports=["soccer"])
    assert len(out) == 1
    (row,) = out.values()
    assert row["1"] == pytest.approx(2.33) and row["2"] == pytest.approx(3.15)
