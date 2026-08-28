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


# ── Totals et handicaps (l'essentiel du volume de l'exchange) ──────────

def _market(mtype, handicap, runners, status="open"):
    return {"market-type": mtype, "status": status, "handicap": handicap,
            "runners": [{"name": n, "prices": _prices(b, l)} for n, b, l in runners]}


def _rich_event(name, markets, live=False):
    from datetime import datetime, timedelta, timezone
    start = (datetime.now(timezone.utc) + timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return {"id": 1, "name": name, "start": start, "in-running-flag": live, "markets": markets}


_ML = _market("one_x_two", None,
              [("Tacuary", 2.30, 2.36), ("Draw", 3.40, 3.50), ("Libertad", 3.10, 3.20)])


def test_totals_main_line_is_the_balanced_one(monkeypatch):
    ev = _rich_event("Tacuary vs Libertad", [_ML,
        _market("total", 5.5, [("OVER 5.5", 14.0, 16.0), ("UNDER 5.5", 1.01, 1.03)]),
        _market("total", 2.5, [("OVER 2.5", 1.87, 1.93), ("UNDER 2.5", 1.95, 2.01)]),
    ])
    _wire(monkeypatch, [ev])
    (row,) = mb.fetch_matchbook_prices(sports=["soccer"]).values()
    principale = {k: v for k, v in row["totals"].items() if k != "ladder"}
    assert principale == {"over": 1.90, "under": 1.98, "point": 2.5}
    # La 5.5 n'entre PAS dans l'échelle : sa fourchette back/lay (14.0 → 16.0)
    # dépasse MAX_SPREAD_RATIO, `_mid` la refuse en amont. L'échelle hérite de
    # tous les filtres de liquidité, elle ne les contourne pas.
    assert [t["point"] for t in row["totals"]["ladder"]] == [2.5]


def test_l_echelle_garde_toutes_les_lignes_liquides(monkeypatch):
    """Une seule ligne retenue par source, et deux sources ne tombent jamais
    d'accord : c'est ce qui laissait `run_engine._meme_ligne` refuser des
    paires que les deux books cotaient pourtant. L'échelle existe pour ça —
    la principale reste en tête, le reste n'est plus perdu."""
    ev = _rich_event("Tacuary vs Libertad", [_ML,
        _market("total", 2.5, [("OVER 2.5", 1.87, 1.93), ("UNDER 2.5", 1.95, 2.01)]),
        _market("total", 3.5, [("OVER 3.5", 2.90, 3.00), ("UNDER 3.5", 1.40, 1.44)]),
        _market("handicap", 0.5, [("Tacuary -0.5", 1.98, 2.04), ("Libertad +0.5", 1.86, 1.92)]),
        _market("handicap", 1.5, [("Tacuary -1.5", 3.30, 3.40), ("Libertad +1.5", 1.33, 1.37)]),
    ])
    _wire(monkeypatch, [ev])
    (row,) = mb.fetch_matchbook_prices(sports=["soccer"]).values()
    assert [t["point"] for t in row["totals"]["ladder"]] == [2.5, 3.5]
    assert row["totals"]["point"] == 2.5
    # Le handicap garde son SIGNE ligne par ligne — une échelle non signée
    # rendrait +0.5 et -0.5 indistinguables, l'erreur qu'A6 a coûté cher.
    assert [h["point"] for h in row["spreads"]["ladder"]] == [-0.5, -1.5]
    assert [h["away_point"] for h in row["spreads"]["ladder"]] == [0.5, 1.5]


def test_handicap_sign_follows_the_runner_not_the_market(monkeypatch):
    """`handicap` du marché n'est pas signé : « Tacuary +1.5 » et
    « Tacuary -1.5 » y sont indistinguables. Le signe vient du runner."""
    ev = _rich_event("Tacuary vs Libertad", [_ML,
        _market("handicap", 1.5, [("Tacuary +1.5", 2.58, 2.62),
                                  ("Libertad -1.5", 1.45, 1.49)]),
    ])
    _wire(monkeypatch, [ev])
    (row,) = mb.fetch_matchbook_prices(sports=["soccer"]).values()
    assert row["spreads"]["point"] == 1.5          # ligne du DOMICILE, signée
    assert row["spreads"]["away_point"] == -1.5
    assert row["spreads"]["home"] == pytest.approx(2.60)
    assert row["spreads"]["away"] == pytest.approx(1.47)


def test_team_name_ending_in_a_number_is_not_read_as_a_line(monkeypatch):
    ev = _rich_event("Schalke 04 vs Iberia 1999", [
        _market("one_x_two", None, [("Schalke 04", 2.30, 2.36), ("Draw", 3.40, 3.50),
                                    ("Iberia 1999", 3.10, 3.20)]),
        _market("handicap", 1.5, [("Schalke 04 -1.5", 2.10, 2.16),
                                  ("Iberia 1999 +1.5", 1.75, 1.81)]),
    ])
    _wire(monkeypatch, [ev])
    (row,) = mb.fetch_matchbook_prices(sports=["soccer"]).values()
    assert row["spreads"]["point"] == -1.5
    assert row["1"] == pytest.approx(2.33)         # le 1X2 reste correct


def test_thin_totals_are_dropped_but_the_match_survives(monkeypatch):
    ev = _rich_event("Tacuary vs Libertad", [_ML,
        _market("total", 2.5, [("OVER 2.5", 110.0, None), ("UNDER 2.5", None, 1.01)]),
    ])
    _wire(monkeypatch, [ev])
    (row,) = mb.fetch_matchbook_prices(sports=["soccer"]).values()
    assert "totals" not in row and row["1"] == pytest.approx(2.33)


def test_suspended_side_market_is_ignored(monkeypatch):
    ev = _rich_event("Tacuary vs Libertad", [_ML,
        _market("total", 2.5, [("OVER 2.5", 1.87, 1.93), ("UNDER 2.5", 1.95, 2.01)],
                status="suspended"),
    ])
    _wire(monkeypatch, [ev])
    (row,) = mb.fetch_matchbook_prices(sports=["soccer"]).values()
    assert "totals" not in row


# ── Sous-marchés : même type, pas le même pari (2026-08-28) ─────────────

def _named(mtype, name, runners):
    return {**_market(mtype, None, runners), "name": name}


class TestLesSousMarchesNeSontPasLeMatchEntier:
    """« Al-Riyadh SC vs Neom SC | SOC Under 2.5 — EV 80.84 % » : le point 2.5
    figurait deux fois dans l'échelle Matchbook — « Total » et « 1st Half
    Total » — et l'alignement par point retenait la mi-temps. Relevé sur
    l'API : quatre marchés de type « total » par match, runners identiques."""

    def _event(self):
        return _rich_event("Al-Riyadh SC vs Neom SC", [
            _ML,
            _named("total", "Total", [("OVER 2.5", 1.57, 1.61), ("UNDER 2.5", 2.64, 2.72)]),
            _named("total", "1st Half Total", [("OVER 2.5", 5.6, 6.0), ("UNDER 2.5", 1.20, 1.22)]),
            _named("total", "Home Team Total Goals", [("OVER 1.5", 2.0, 2.1), ("UNDER 1.5", 1.85, 1.95)]),
            _named("total", "Away Team Total Goals", [("OVER 1.5", 2.3, 2.4), ("UNDER 1.5", 1.65, 1.7)]),
            _named("handicap", "Handicap", [("Al-Riyadh SC -0.5", 2.0, 2.1), ("Neom SC +0.5", 1.85, 1.95)]),
            _named("handicap", "1st Half Handicap", [("Al-Riyadh SC -0.5", 3.5, 3.7), ("Neom SC +0.5", 1.3, 1.32)]),
        ])

    def test_un_point_napparait_quune_fois_dans_lechelle(self):
        tot = mb._totals_odds(self._event())
        points = [r["point"] for r in tot["ladder"]]
        assert len(points) == len(set(points)), points
        assert points == [2.5]
        assert tot["under"] == pytest.approx(2.68), "c'est le Under du match entier, pas de la mi-temps"

    def test_le_handicap_de_mi_temps_est_ecarte_aussi(self):
        hcp = mb._handicap_odds(self._event(), "Al-Riyadh SC", "Neom SC")
        assert len(hcp["ladder"]) == 1
        assert hcp["home"] == pytest.approx(2.05)

    def test_un_marche_sans_nom_reste_accepte(self):
        # L'API a toujours nommé ses marchés ; si elle cessait, on ne veut pas
        # perdre la seule source de totals sharp du stack sur une clé absente.
        ev = _rich_event("A vs B", [_ML, _market("total", None,
                                                 [("OVER 2.5", 1.9, 1.95), ("UNDER 2.5", 1.95, 2.0)])])
        assert mb._totals_odds(ev)["point"] == 2.5

    @pytest.mark.parametrize("nom", ["1st Half Total", "2nd Half Total", "Home Team Total Goals",
                                     "Away Team Total Goals", "Total Corners", "Total Cards",
                                     "1st Quarter Total", "Bookings Total"])
    def test_chaque_qualificatif_releve_est_un_sous_marche(self, nom):
        assert mb._est_sous_marche({"name": nom})

    @pytest.mark.parametrize("nom", ["Total", "Handicap", "Total Goals", "Total Points"])
    def test_le_marche_du_match_entier_ne_lest_pas(self, nom):
        assert not mb._est_sous_marche({"name": nom})
