"""
tests/test_odds_api_io.py — core/odds_api_io.py.

Cette source redonne accès au prix SOFT (1xbet & co.) par une voie
authentifiée, là où le LineFeed des mêmes books est filtré par IP depuis les
runners GitHub. Formes de réponse vérifiées en direct le 2026-08-20 avec la
clé du projet.

Ce qui est verrouillé ici :
- le COÛT : 1 requête calendrier + 1 par LOT DE DIX événements
  (`/v3/odds/multi`), jamais une par match — c'est ce qui rend 500 req/jour
  suffisantes pour ~40 scans ;
- le budget journalier partagé, qui s'arrête AVANT d'appeler ;
- le choix de la LIGNE PRINCIPALE parmi la douzaine de handicaps/totaux
  publiés : prendre la première retiendrait une ligne extrême cotée
  1.01/8.60, sans rapport avec celle que le moteur compare au sharp ;
- la conversion vers les formes exactes du moteur (`odds_1xbet`,
  `spreads_1xbet` avec `point`/`away_point`, `totals_1xbet`) ;
- un book sharp parmi les sélectionnés ressort en `odds_pinnacle` ;
- seuls les matchs `pending` entrent dans un scan pré-match.
"""
import logging

import pytest

import core.odds_api_io as oai


class _Resp:
    def __init__(self, payload, status=200):
        self._payload, self.status_code = payload, status
        self.text = "" if payload is None else "body"

    def json(self):
        if self._payload is None:
            raise ValueError("illisible")
        return self._payload


def _event(eid, home, away, status="pending", date="2030-01-01T18:00:00Z"):
    return {"id": eid, "home": home, "away": away, "status": status, "date": date,
            "league": {"name": "Ligue Test"}, "sport": {"slug": "football"}}


def _ml(home, draw, away):
    return {"name": "ML", "odds": [{"home": str(home), "draw": str(draw), "away": str(away)}]}


def _spread(rows):
    return {"name": "Spread", "odds": [{"hdp": h, "home": str(a), "away": str(b)} for h, a, b in rows]}


def _totals(rows):
    return {"name": "Totals", "odds": [{"hdp": h, "over": str(a), "under": str(b)} for h, a, b in rows]}


def _odds_event(eid, home, away, books):
    ev = _event(eid, home, away)
    ev["bookmakers"] = books
    return ev


def _wire(monkeypatch, events, odds_by_batch, *, books=("1xbet",),
          events_status=200, odds_status=200):
    """odds_by_batch : liste de réponses, une par appel /odds/multi."""
    calls = {"events": 0, "multi": [], "selected": 0}
    pending = list(odds_by_batch)

    def fake_get(url, timeout=None, params=None):
        if url.endswith("/bookmakers/selected"):
            calls["selected"] += 1
            return _Resp({"bookmakers": list(books), "count": len(books)})
        if url.endswith("/events"):
            calls["events"] += 1
            if events_status != 200:
                return _Resp(None, status=events_status)
            return _Resp(events)
        if url.endswith("/odds/multi"):
            calls["multi"].append(params["eventIds"].split(","))
            if odds_status != 200:
                return _Resp(None, status=odds_status)
            return _Resp(pending.pop(0) if pending else [])
        raise AssertionError(f"URL inattendue : {url}")

    monkeypatch.setattr(oai.requests, "get", fake_get)
    oai.reset_cache()
    return calls


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("ODDS_API_IO_BOOKMAKERS", raising=False)
    monkeypatch.setattr(oai, "get_secret", lambda name, **kw: None)
    oai.reset_cache()
    yield
    oai.reset_cache()


def test_no_key_means_no_network(monkeypatch):
    touched = []
    monkeypatch.setattr(oai.requests, "get", lambda *a, **k: touched.append(a))
    assert oai.fetch_sport("soccer") == []
    assert touched == []


def test_unknown_sport_is_ignored():
    assert oai.fetch_sport("quidditch", api_key="k") == []


def test_cost_is_one_events_call_plus_one_per_ten_events(monkeypatch):
    events = [_event(i, f"H{i}", f"A{i}") for i in range(25)]
    batches = [[_odds_event(i, f"H{i}", f"A{i}", {"1xbet": [_ml(2.0, 3.4, 3.6)]})
                for i in range(s, min(s + 10, 25))] for s in (0, 10, 20)]
    calls = _wire(monkeypatch, events, batches)
    out = oai.fetch_sport("soccer", api_key="k")
    assert calls["events"] == 1
    assert [len(b) for b in calls["multi"]] == [10, 10, 5]
    assert len(out) == 25


def test_only_pending_events_are_scanned(monkeypatch):
    events = [_event(1, "A", "B", status="settled"), _event(2, "C", "D", status="live"),
              _event(3, "E", "F", status="pending")]
    calls = _wire(monkeypatch, events,
                  [[_odds_event(3, "E", "F", {"1xbet": [_ml(2.0, 3.4, 3.6)]})]])
    out = oai.fetch_sport("soccer", api_key="k")
    assert calls["multi"] == [["3"]]
    assert [m["match"] for m in out] == ["E vs F"]


def test_markets_map_to_engine_shapes(monkeypatch):
    ev = _odds_event(1, "Getafe", "Partizan", {"1xbet": [
        _ml(1.34, 4.8, 10.0),
        _spread([(-1.25, 1.88, 1.92), (-1.5, 2.136, 1.71), (-1.0, 1.53, 2.21)]),
        _totals([(2.25, 1.89, 1.91), (5.5, 15.0, 1.016)]),
    ]})
    _wire(monkeypatch, [_event(1, "Getafe", "Partizan")], [[ev]])
    (m,) = oai.fetch_sport("soccer", api_key="k")
    assert m["odds_1xbet"] == {"1": 1.34, "X": 4.8, "2": 10.0}
    # ligne principale = prix les plus proches, pas la première publiée
    principale = lambda d: {k: v for k, v in d.items() if k != "ladder"}
    assert principale(m["spreads_1xbet"]) == {"home": 1.88, "away": 1.92,
                                              "point": -1.25, "away_point": 1.25}
    assert principale(m["totals_1xbet"]) == {"over": 1.89, "under": 1.91, "point": 2.25}
    # L'échelle garde TOUTES les lignes cotées, la principale en tête —
    # sans elle, deux sources qui choisissent chacune la leur ne se
    # rencontrent jamais (voir run_engine._aligner_sur_meme_ligne).
    assert [r["point"] for r in m["spreads_1xbet"]["ladder"]] == [-1.25, -1.5, -1.0]
    assert [r["away_point"] for r in m["spreads_1xbet"]["ladder"]] == [1.25, 1.5, 1.0]
    assert [r["point"] for r in m["totals_1xbet"]["ladder"]] == [2.25, 5.5]
    assert m["sport"] == "soccer" and m["sport_id"] == 1
    assert m["commence_time"] == "2030-01-01T18:00:00Z"


def test_extreme_line_is_never_taken_as_the_main_one(monkeypatch):
    """Une ligne à 1.01/8.60 existe toujours dans le catalogue du book."""
    ev = _odds_event(1, "A", "B", {"1xbet": [
        _ml(2.0, 3.4, 3.6),
        _totals([(5.5, 15.0, 1.016), (1.25, 1.195, 3.72), (2.5, 1.9, 1.9)]),
    ]})
    _wire(monkeypatch, [_event(1, "A", "B")], [[ev]])
    (m,) = oai.fetch_sport("soccer", api_key="k")
    assert m["totals_1xbet"]["point"] == 2.5


def test_no_draw_outside_soccer(monkeypatch):
    ev = _odds_event(1, "Fever", "Wings", {"1xbet": [_ml(2.6, 12.0, 1.55)]})
    _wire(monkeypatch, [_event(1, "Fever", "Wings")], [[ev]])
    (m,) = oai.fetch_sport("basketball", api_key="k")
    assert m["odds_1xbet"]["X"] == 0.0


def test_sharp_book_becomes_odds_pinnacle(monkeypatch):
    ev = _odds_event(1, "A", "B", {
        "1xbet": [_ml(2.30, 3.40, 3.10)],
        "Betfair Exchange": [_ml(2.42, 3.55, 3.20)],
    })
    _wire(monkeypatch, [_event(1, "A", "B")], [[ev]], books=("1xbet", "Betfair Exchange"))
    (m,) = oai.fetch_sport("soccer", api_key="k")
    assert m["odds_pinnacle"] == {"1": 2.42, "X": 3.55, "2": 3.20}
    assert m["odds_1xbet"] == {"1": 2.30, "X": 3.40, "2": 3.10}


def test_event_without_moneyline_is_dropped(monkeypatch):
    ev = _odds_event(1, "A", "B", {"1xbet": [_totals([(2.5, 1.9, 1.9)])]})
    _wire(monkeypatch, [_event(1, "A", "B")], [[ev]])
    assert oai.fetch_sport("soccer", api_key="k") == []


# ── Budget et bookmakers ──────────────────────────────────────────────

def test_daily_budget_stops_before_any_call(monkeypatch, caplog):
    touched = []
    monkeypatch.setattr(oai.requests, "get", lambda *a, **k: touched.append(a))
    monkeypatch.setattr(oai.daily_quota, "spent", lambda bucket: oai.DAILY_BUDGET)
    with caplog.at_level(logging.WARNING, logger="PREDATOR.odds_api_io"):
        assert oai.fetch_sport("soccer", api_key="k") == []
    assert touched == []
    assert any("budget journalier" in r.getMessage() for r in caplog.records)


def test_budget_exhausted_mid_cycle_keeps_what_was_fetched(monkeypatch):
    events = [_event(i, f"H{i}", f"A{i}") for i in range(20)]
    batches = [[_odds_event(i, f"H{i}", f"A{i}", {"1xbet": [_ml(2.0, 3.4, 3.6)]})
                for i in range(0, 10)]]
    _wire(monkeypatch, events, batches)
    seq = iter([0, 0, oai.DAILY_BUDGET, oai.DAILY_BUDGET])
    # Le compteur TOTAL suit la séquence ; les compteurs PAR COMPTE (pool,
    # 2026-08-28) restent à zéro — c'est le total qui coupe ici.
    monkeypatch.setattr(oai.daily_quota, "spent",
                        lambda bucket: next(seq, oai.DAILY_BUDGET) if bucket == oai.QUOTA_BUCKET else 0)
    out = oai.fetch_sport("soccer", api_key="k")
    assert len(out) == 10          # le premier lot est conservé, pas jeté


def test_selected_bookmakers_come_from_the_account(monkeypatch):
    calls = _wire(monkeypatch, [], [], books=("1xbet", "Betano"))
    assert oai.selected_bookmakers("k") == ["1xbet", "Betano"]
    assert oai.selected_bookmakers("k") == ["1xbet", "Betano"]
    assert calls["selected"] == 1          # mémorisé pour le processus


def test_env_override_skips_the_lookup(monkeypatch):
    calls = _wire(monkeypatch, [], [])
    monkeypatch.setenv("ODDS_API_IO_BOOKMAKERS", "1xbet, Winamax FR")
    assert oai.selected_bookmakers("k") == ["1xbet", "Winamax FR"]
    assert calls["selected"] == 0


def test_no_selected_bookmaker_means_no_odds_call(monkeypatch, caplog):
    calls = _wire(monkeypatch, [_event(1, "A", "B")], [], books=())
    with caplog.at_level(logging.WARNING, logger="PREDATOR.odds_api_io"):
        assert oai.fetch_sport("soccer", api_key="k") == []
    assert calls["multi"] == [] and calls["events"] == 0
    assert any("aucun bookmaker" in r.getMessage() for r in caplog.records)


# ── Robustesse ────────────────────────────────────────────────────────

@pytest.mark.parametrize("status", [401, 429, 500])
def test_events_http_error_is_logged_and_empty(monkeypatch, caplog, status):
    _wire(monkeypatch, [], [], events_status=status)
    with caplog.at_level(logging.WARNING, logger="PREDATOR.odds_api_io"):
        assert oai.fetch_sport("soccer", api_key="k") == []
    assert any(f"HTTP {status}" in r.getMessage() for r in caplog.records)


def test_odds_http_error_stops_the_cycle(monkeypatch):
    events = [_event(i, f"H{i}", f"A{i}") for i in range(20)]
    calls = _wire(monkeypatch, events, [], odds_status=500)
    assert oai.fetch_sport("soccer", api_key="k") == []
    assert len(calls["multi"]) == 1        # on n'insiste pas sur les lots suivants


def test_network_error_is_swallowed(monkeypatch):
    def boom(*a, **k):
        raise ConnectionError("réseau")
    monkeypatch.setattr(oai.requests, "get", boom)
    assert oai.fetch_sport("soccer", api_key="k") == []


def test_fetch_all_isolates_a_failing_sport(monkeypatch):
    def flaky(sport, hours_ahead=24):
        if sport == "basketball":
            raise RuntimeError("boom")
        return [{"match": f"{sport} match"}]

    monkeypatch.setattr(oai, "fetch_sport", flaky)
    out = oai.fetch_all(sports=["soccer", "basketball", "hockey"])
    assert {m["match"] for m in out} == {"soccer match", "hockey match"}


# ── Pool de comptes (2026-08-28) ──────────────────────────────────────
# Le plan gratuit se compte PAR COMPTE (500 req, 2 books). Le pool suit le
# contrat de core/odds_api.candidate_keys : ordonné, dédupliqué, compte
# refusé écarté et requête rejouée sur le suivant, budget par compte.

def _wire_pool(monkeypatch, events, odds_payload, *, refus=None, books_by_key=None):
    """refus : {clé: status HTTP} — le compte répond ce code à /odds/multi.
    books_by_key : {clé: [...]} — sélection propre à chaque compte."""
    refus = refus or {}
    books_by_key = books_by_key or {}
    calls = []          # (path, clé, bookmakers)

    def fake_get(url, timeout=None, params=None):
        key = params["apiKey"]
        path = url.rsplit("/v3/", 1)[1]
        calls.append((path, key, params.get("bookmakers")))
        if path == "bookmakers/selected":
            return _Resp({"bookmakers": list(books_by_key.get(key, ["1xbet"]))})
        if path == "events":
            return _Resp(events)
        if key in refus:
            return _Resp(None, status=refus[key])
        return _Resp(odds_payload)

    monkeypatch.setattr(oai.requests, "get", fake_get)
    oai.reset_cache()
    return calls


def test_le_pool_lit_KEYS_puis_KEY_et_dedoublonne(monkeypatch):
    monkeypatch.setattr(oai, "get_secret",
                        lambda name, **kw: {"ODDS_API_IO_KEYS": "a, b;b", "ODDS_API_IO_KEY": "c"}.get(name))
    monkeypatch.setenv("ODDS_API_IO_KEY_2", "a")
    assert oai.candidate_keys() == ["a", "b", "c"]
    assert oai.candidate_keys("z") == ["z", "a", "b", "c"]     # l'explicite d'abord


def test_un_compte_refuse_est_ecarte_et_le_meme_lot_rejoue_sur_le_suivant(monkeypatch):
    events = [_event(1, "A", "B")]
    payload = [_odds_event(1, "A", "B", {"1xbet": [_ml(2.0, 3.4, 3.6)]})]
    calls = _wire_pool(monkeypatch, events, payload, refus={"k1": 429})
    out = oai.fetch_sport("soccer", api_key="k1,k2")
    assert [m["match"] for m in out] == ["A vs B"]
    multi = [(k, b) for p, k, b in calls if p == "odds/multi"]
    assert multi == [("k1", "1xbet"), ("k2", "1xbet")], "même lot, compte suivant"
    assert "k1" in oai._dead_keys


def test_les_books_sont_ceux_du_compte_qui_sert(monkeypatch):
    events = [_event(1, "A", "B")]
    payload = [_odds_event(1, "A", "B", {"Betano": [_ml(2.0, 3.4, 3.6)]})]
    calls = _wire_pool(monkeypatch, events, payload, refus={"k1": 401},
                       books_by_key={"k1": ["1xbet", "Bet365"], "k2": ["Betano", "Unibet"]})
    oai.fetch_sport("soccer", api_key="k1,k2")
    multi = [(k, b) for p, k, b in calls if p == "odds/multi"]
    assert multi[-1] == ("k2", "Betano,Unibet")


def test_le_budget_est_tenu_par_compte(monkeypatch):
    # Compte #1 à son plafond du jour, #2 intact : le cycle part sur #2 sans
    # qu'aucune requête ne soit payée sur #1.
    events = [_event(1, "A", "B")]
    payload = [_odds_event(1, "A", "B", {"1xbet": [_ml(2.0, 3.4, 3.6)]})]
    calls = _wire_pool(monkeypatch, events, payload)
    plein = oai._bucket("k1")
    monkeypatch.setattr(oai.daily_quota, "spent",
                        lambda bucket: oai.DAILY_BUDGET if bucket == plein else 0)
    out = oai.fetch_sport("soccer", api_key="k1,k2")
    assert len(out) == 1
    assert {k for _p, k, _b in calls} == {"k2"}


def test_le_rythme_porte_sur_le_total_des_comptes(monkeypatch):
    # Deux comptes = 2 × DAILY_BUDGET : le rythme s'ouvre sur ce total.
    seen = {}
    monkeypatch.setattr(oai.daily_quota, "paced_allowance",
                        lambda budget, floor, now=None: seen.setdefault("budget", budget) and 0)
    _wire_pool(monkeypatch, [], [])
    oai.fetch_sport("soccer", api_key="k1,k2")
    assert seen["budget"] == 2 * oai.DAILY_BUDGET


def test_tous_les_comptes_refuses_rend_vide_sans_exception(monkeypatch, caplog):
    events = [_event(1, "A", "B")]
    calls = _wire_pool(monkeypatch, events, [], refus={"k1": 403, "k2": 401})
    with caplog.at_level(logging.WARNING, logger="PREDATOR.odds_api_io"):
        assert oai.fetch_sport("soccer", api_key="k1,k2") == []
    assert sum(1 for p, _k, _b in calls if p == "odds/multi") == 2
    assert sum("écarté" in r.getMessage() for r in caplog.records) == 2


def test_probe_rend_un_etat_par_compte(monkeypatch):
    _wire_pool(monkeypatch, [], [], books_by_key={"k1": ["1xbet", "Bet365"], "k2": []})
    ok, detail = oai.probe("k1,k2")
    assert ok and "2 compte(s)" in detail and "#1: books=['1xbet', 'Bet365']" in detail
