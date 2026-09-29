"""
tests/test_football_americain.py — la NFL rendue exécutable (2026-09-29).

Du 2026-09-10 au 2026-09-29 la NFL a été PAYÉE à OddsAPI à chaque créneau
sans produire un seul match : OddsAPI n'y cote pas 1xbet (`ops.py books
americanfootball_nfl` : onexbet 0/16), `_parse_event` jetait tout, et rien
ne le disait. Ces gardiens tiennent les trois morceaux du correctif :

1. un crédit payé sans retour se DIT, avec sa cause (`PAYÉ SANS RETOUR`) ;
2. odds-api.io apporte le côté exécutable (1xbet/Bet365), routé par ligue :
   NFL → americanfootball, NCAA → college_football, CFL et présaison hors
   périmètre, écartées AVANT d'être payées ;
3. Matchbook et Smarkets sont interrogés sur le football américain — le
   prix sharp sans lequel un match odds-api.io est un « MARCHÉ MORT ».

Règle 13 : budget chiffré et critère de retrait daté dans la docstring de
core/odds_api_io — ce fichier tombe si l'un disparaît sans l'autre.
"""
import logging
from pathlib import Path

import pytest

import core.odds_api as odds_api
import core.odds_api_io as oai

RACINE = Path(__file__).resolve().parent.parent


# ── 1. Payé sans retour ────────────────────────────────────────────────

class _RespOA:
    def __init__(self, payload, status=200):
        self._payload, self.status_code = payload, status
        self.headers = {"x-requests-remaining": "400", "x-requests-used": "100"}

    def json(self):
        return self._payload


def _nfl_sans_1xbet():
    """Forme réelle relevée le 2026-09-29 : Pinnacle et vingt books, pas onexbet."""
    return [{"home_team": "Cleveland Browns", "away_team": "Pittsburgh Steelers",
             "commence_time": "2030-01-01T00:15:00Z",
             "bookmakers": [
                 {"key": "pinnacle", "markets": [{"key": "h2h", "outcomes": [
                     {"name": "Cleveland Browns", "price": 2.45},
                     {"name": "Pittsburgh Steelers", "price": 1.62}]}]},
                 {"key": "williamhill", "markets": [{"key": "h2h", "outcomes": [
                     {"name": "Cleveland Browns", "price": 2.40},
                     {"name": "Pittsburgh Steelers", "price": 1.60}]}]}]}]


@pytest.fixture
def _sans_supabase(monkeypatch):
    """Le pool de clés lit `app_secrets` : sans ce patch, le test ouvrirait
    un client Supabase (tests purs uniquement)."""
    monkeypatch.setattr(odds_api, "get_secret", lambda name, **kw: None)


def test_une_ligue_payee_sans_match_exploitable_le_dit_avec_sa_cause(monkeypatch, caplog,
                                                                      _sans_supabase):
    def fake_get(url, params=None, timeout=None):
        if url.rstrip("/").endswith("/sports"):
            return _RespOA([])
        if "/events" in url:
            return _RespOA([{"id": "x", "commence_time": "2030-01-01T00:15:00Z"}])
        return _RespOA(_nfl_sans_1xbet())

    monkeypatch.setattr(odds_api.requests, "get", fake_get)
    with caplog.at_level(logging.WARNING, logger="PREDATOR.odds_api"):
        out = odds_api.fetch_odds(api_key="k", hours_ahead=24,
                                  sport_keys={"americanfootball_nfl": "americanfootball"})
    assert out == []
    msgs = [r.getMessage() for r in caplog.records if "PAYÉ SANS RETOUR" in r.getMessage()]
    assert msgs, "un crédit payé pour rien doit laisser une trace"
    assert "americanfootball_nfl" in msgs[0]
    assert "sans pinnacle : 0/1" in msgs[0]
    assert "sans book d'exécution" in msgs[0] and ": 1/1" in msgs[0]


def test_une_ligue_payee_qui_rend_ses_matchs_ne_crie_pas(monkeypatch, caplog, _sans_supabase):
    ev = _nfl_sans_1xbet()[0]
    ev["bookmakers"].append({"key": odds_api.XBET_KEY, "markets": [{"key": "h2h", "outcomes": [
        {"name": "Cleveland Browns", "price": 2.50},
        {"name": "Pittsburgh Steelers", "price": 1.58}]}]})

    def fake_get(url, params=None, timeout=None):
        if url.rstrip("/").endswith("/sports"):
            return _RespOA([])
        if "/events" in url:
            return _RespOA([{"id": "x", "commence_time": "2030-01-01T00:15:00Z"}])
        return _RespOA([ev])

    monkeypatch.setattr(odds_api.requests, "get", fake_get)
    with caplog.at_level(logging.WARNING, logger="PREDATOR.odds_api"):
        out = odds_api.fetch_odds(api_key="k", hours_ahead=24,
                                  sport_keys={"americanfootball_nfl": "americanfootball"})
    assert len(out) == 1
    assert not any("PAYÉ SANS RETOUR" in r.getMessage() for r in caplog.records)


# ── 2. odds-api.io : le côté exécutable, routé par ligue ───────────────

@pytest.mark.parametrize("ligue, attendu", [
    ("USA - NFL", "americanfootball"),
    ("USA - College", "college_football"),
    ("USA - NCAA Division I FBS", "college_football"),
    ("Canada - CFL", None),
    ("USA - NFL Preseason", None),
    ("USA - UFL", None),
    ("", None),
])
def test_chaque_ligue_retrouve_son_sport_du_moteur(ligue, attendu):
    assert oai.sport_de_ligue("americanfootball", ligue) == attendu


def test_un_sport_non_route_garde_son_nom():
    assert oai.sport_de_ligue("soccer", "Canada - CFL") == "soccer"


class _RespIO:
    def __init__(self, payload, status=200):
        self._payload, self.status_code = payload, status
        self.text = "body"

    def json(self):
        return self._payload


def _ev(eid, home, away, ligue, date="2030-01-01T18:00:00Z"):
    return {"id": eid, "home": home, "away": away, "status": "pending", "date": date,
            "league": {"name": ligue}}


def _ml(h, a):
    return {"name": "ML", "odds": [{"home": str(h), "away": str(a)}]}


@pytest.fixture
def _io(monkeypatch):
    monkeypatch.delenv("ODDS_API_IO_BOOKMAKERS", raising=False)
    monkeypatch.setattr(oai, "get_secret", lambda name, **kw: None)
    oai.reset_cache()
    yield
    oai.reset_cache()


def _cabler_io(monkeypatch, events, cotes):
    calls = {"multi": []}

    def fake_get(url, timeout=None, params=None):
        if url.endswith("/bookmakers/selected"):
            return _RespIO({"bookmakers": ["Bet365", "1xbet"]})
        if url.endswith("/events"):
            calls["sport"] = params.get("sport")
            return _RespIO(events)
        if url.endswith("/odds/multi"):
            ids = params["eventIds"].split(",")
            calls["multi"].append(ids)
            return _RespIO([c for c in cotes if str(c["id"]) in ids])
        raise AssertionError(url)

    monkeypatch.setattr(oai.requests, "get", fake_get)
    return calls


def test_la_cfl_nest_jamais_payee_et_la_nfl_passe_avant_la_ncaa(monkeypatch, _io):
    evs = [_ev(1, "Ottawa Redblacks", "Hamilton Tiger-Cats", "Canada - CFL", "2030-01-01T17:00:00Z"),
           _ev(2, "Tulsa", "North Texas", "USA - College", "2030-01-01T17:30:00Z"),
           _ev(3, "Cleveland Browns", "Pittsburgh Steelers", "USA - NFL", "2030-01-01T20:00:00Z")]
    cotes = [{**e, "bookmakers": {"1xbet": [_ml(2.5, 1.58)]}} for e in evs]
    calls = _cabler_io(monkeypatch, evs, cotes)
    out = oai.fetch_sport("americanfootball", api_key="k", max_events=1)
    assert calls["sport"] == "american-football"
    assert calls["multi"] == [["3"]], "NFL d'abord ; la CFL n'entre même pas dans la file"
    (m,) = out
    assert m["sport"] == "americanfootball" and m["odds_1xbet"]["X"] == 0.0


def test_un_match_ncaa_part_en_college_football(monkeypatch, _io):
    evs = [_ev(2, "Tulsa", "North Texas", "USA - College")]
    cotes = [{**evs[0], "bookmakers": {"Bet365": [_ml(2.0, 1.8)], "1xbet": [_ml(2.02, 1.79)]}}]
    _cabler_io(monkeypatch, evs, cotes)
    (m,) = oai.fetch_sport("americanfootball", api_key="k")
    assert m["sport"] == "college_football"
    assert set(m["h2h_par_book"]) == {"1xbet", "bet365"}


def test_budget_et_critere_de_retrait_sont_ecrits():
    """Règle 13 (AUDIT.md §3bis) : budget chiffré, critère daté, même commit."""
    doc = oai.__doc__ or ""
    bloc = doc.split("FOOTBALL AMÉRICAIN", 1)[1]
    assert "BUDGET" in bloc and "32 req/j" in bloc
    assert "CRITÈRE DE RETRAIT" in bloc and "2026-10-27" in bloc
    assert oai.SPORTS["americanfootball"][0] == "american-football"
    assert oai.cap_pour("americanfootball") == 30
    # 1 calendrier + ceil(30/10) cotes = 4 req/scan, soit 32/j sur 8 scans
    assert 1 + -(-oai.cap_pour("americanfootball") // oai.MULTI_BATCH) == 4


def test_le_harvester_interroge_le_football_americain():
    from core import harvester
    assert harvester._ODDS_API_IO_BY_ID.get(oai.SPORTS["americanfootball"][1]) == "americanfootball"


# ── 3. Le prix sharp : les exchanges sont interrogés ───────────────────

def test_matchbook_et_smarkets_sont_interroges_sur_le_football_americain():
    from core.matchbook import SPORT_IDS
    from core.smarkets import EVENT_TYPES
    assert "americanfootball" in SPORT_IDS and "americanfootball" in EVENT_TYPES
    moteur = (RACINE / "run_engine.py").read_text(encoding="utf-8")
    for appel in ("fetch_matchbook_prices(", "fetch_smarkets_prices("):
        bloc = moteur.split(appel, 1)[1].split("hours_ahead=", 1)[0]
        assert '"americanfootball"' in bloc, f"{appel} ne demande pas le football américain"


def test_la_sonde_des_books_existe():
    ops = (RACINE / "scripts" / "ops.py").read_text(encoding="utf-8")
    assert "def books(" in ops and 'cmd == "books"' in ops
