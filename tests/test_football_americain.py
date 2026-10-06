"""
tests/test_football_americain.py — la NFL rendue exécutable (2026-09-29).

Du 2026-09-10 au 2026-09-29 la NFL a été PAYÉE à OddsAPI à chaque créneau
sans produire un seul match : OddsAPI n'y cote pas 1xbet (`ops.py books
americanfootball_nfl` : onexbet 0/16), `_parse_event` jetait tout, et rien
ne le disait. Ces gardiens tiennent les trois morceaux du correctif :

1. un crédit payé sans retour se DIT, avec sa cause (`PAYÉ SANS RETOUR`) ;
2. odds-api.io apporte le côté exécutable (1xbet/Bet365), routé par ligue :
   NFL → americanfootball ; NCAA (retirée le 2026-10-06, décision
   opérateur), CFL et présaison hors périmètre, écartées AVANT d'être payées ;
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


def _cabler_oddsapi(monkeypatch, payload):
    def fake_get(url, params=None, timeout=None):
        if url.rstrip("/").endswith("/sports"):
            return _RespOA([])
        if "/events" in url:
            return _RespOA([{"id": "x", "commence_time": "2030-01-01T00:15:00Z"}])
        return _RespOA(payload)

    monkeypatch.setattr(odds_api.requests, "get", fake_get)


def test_le_pinnacle_paye_sans_1xbet_est_garde_comme_reference(monkeypatch, caplog,
                                                                _sans_supabase):
    """Décision opérateur du 2026-09-29 : « continue à payer, mais il me faut
    des matchs exploitables ». Le crédit NFL achète désormais le prix sharp."""
    _cabler_oddsapi(monkeypatch, _nfl_sans_1xbet())
    with caplog.at_level(logging.INFO, logger="PREDATOR.odds_api"):
        out = odds_api.fetch_odds(api_key="k", hours_ahead=24,
                                  sport_keys={"americanfootball_nfl": "americanfootball"})
    assert out == [], "pas de book d'exécution : pas un match du Tier 1"
    pool = odds_api.sharp_sans_execution()
    row = pool["cleveland browns_pittsburgh steelers"]
    assert (row["1"], row["2"], row["_source"]) == (2.45, 1.62, "pinnacle")
    msgs = [r.getMessage() for r in caplog.records]
    assert any("PAYÉ POUR LE SHARP" in m and "sans book d'exécution" in m for m in msgs)
    assert not any("PAYÉ SANS RETOUR" in m for m in msgs)


def test_sans_pinnacle_circa_prend_le_relais():
    ev = _nfl_sans_1xbet()[0]
    ev["bookmakers"] = [{"key": odds_api.CIRCA_KEY, "markets": [{"key": "h2h", "outcomes": [
        {"name": "Cleveland Browns", "price": 2.40}, {"name": "Pittsburgh Steelers", "price": 1.63}]}]}]
    assert odds_api.sharp_seul(ev, "americanfootball")["_source"] == "circa"


def test_une_ligue_payee_sans_rien_d_utilisable_le_dit_avec_sa_cause(monkeypatch, caplog,
                                                                      _sans_supabase):
    ev = _nfl_sans_1xbet()[0]
    ev["bookmakers"] = [b for b in ev["bookmakers"] if b["key"] != "pinnacle"]
    _cabler_oddsapi(monkeypatch, [ev])
    with caplog.at_level(logging.WARNING, logger="PREDATOR.odds_api"):
        assert odds_api.fetch_odds(api_key="k", hours_ahead=24,
                                   sport_keys={"americanfootball_nfl": "americanfootball"}) == []
    msgs = [r.getMessage() for r in caplog.records if "PAYÉ SANS RETOUR" in r.getMessage()]
    assert msgs and "sans pinnacle : 1/1" in msgs[0] and ": 1/1" in msgs[0]
    assert odds_api.sharp_sans_execution() == {}


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
    # NCAA retirée le 2026-10-06 (décision opérateur, LIGUES_RETIREES).
    ("USA - College", None),
    ("USA - NCAA Division I FBS", None),
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


def test_ni_la_cfl_ni_la_ncaa_ne_sont_payees(monkeypatch, _io):
    evs = [_ev(1, "Ottawa Redblacks", "Hamilton Tiger-Cats", "Canada - CFL", "2030-01-01T17:00:00Z"),
           _ev(2, "Tulsa", "North Texas", "USA - College", "2030-01-01T17:30:00Z"),
           _ev(3, "Cleveland Browns", "Pittsburgh Steelers", "USA - NFL", "2030-01-01T20:00:00Z")]
    cotes = [{**e, "bookmakers": {"1xbet": [_ml(2.5, 1.58)]}} for e in evs]
    calls = _cabler_io(monkeypatch, evs, cotes)
    out = oai.fetch_sport("americanfootball", api_key="k", max_events=1)
    assert calls["sport"] == "american-football"
    assert calls["multi"] == [["3"]], "NFL seule ; CFL et NCAA n'entrent même pas dans la file"
    (m,) = out
    assert m["sport"] == "americanfootball" and m["odds_1xbet"]["X"] == 0.0


def test_un_match_ncaa_nest_plus_emis_ni_paye(monkeypatch, _io):
    """Décision opérateur du 2026-10-06 (« Supprimer ncaa, garde nfl ») :
    odds-api.io était le SEUL chemin d'émission de la NCAA (OddsAPI n'y cote
    pas 1xbet). Un calendrier 100 % universitaire ne coûte plus une requête
    de cotes et ne rend aucun match."""
    evs = [_ev(2, "Tulsa", "North Texas", "USA - College"),
           _ev(4, "Ohio State", "Michigan", "USA - NCAA Division I FBS")]
    cotes = [{**e, "bookmakers": {"Bet365": [_ml(2.0, 1.8)], "1xbet": [_ml(2.02, 1.79)]}}
             for e in evs]
    calls = _cabler_io(monkeypatch, evs, cotes)
    assert oai.fetch_sport("americanfootball", api_key="k") == []
    assert calls["multi"] == []


def test_la_ncaa_est_retiree_et_la_nfl_gardee():
    from core.odds_api import LIGUES_RETIREES, SPORT_KEYS, sports_au_perimetre
    assert "americanfootball_ncaaf" not in SPORT_KEYS
    assert "americanfootball_ncaaf" in LIGUES_RETIREES
    assert SPORT_KEYS["americanfootball_nfl"] == "americanfootball"
    assert "college_football" not in sports_au_perimetre()
    assert "americanfootball" in sports_au_perimetre()
    assert "college_football" not in {c for r in oai.ROUTAGE_PAR_LIGUE.values() for _, c in r}


def test_les_lignes_ncaa_passees_restent_reglables():
    """Règle 9 : rien n'est effacé, le règlement ESPN du sport-type reste."""
    from core.score_sources import sports_reglables
    assert "college_football" in sports_reglables()


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


# ── 4. Le chemin entier : du harvest au match réglable ─────────────────

def test_un_match_nfl_odds_api_io_devient_vivant_grace_au_pinnacle_paye():
    """odds-api.io apporte 1xbet, OddsAPI le Pinnacle payé : le match a son
    prix sharp, et la garde « marché vivant » le laisse passer."""
    import run_engine as eng
    m = {"match": "Cleveland Browns vs Pittsburgh Steelers", "home": "Cleveland Browns",
         "away": "Pittsburgh Steelers", "sport": "americanfootball",
         "odds_1xbet": {"1": 2.55, "X": 0.0, "2": 1.57}, "_soft_source": "odds_api_io"}
    pool = {"cleveland browns_pittsburgh steelers": odds_api.sharp_seul(
        _nfl_sans_1xbet()[0], "americanfootball")}

    class _Log:
        def info(self, *a, **k): pass
        warning = info

    assert eng._enrich_from_exchange([m], pool, _Log()) == 1
    assert m["odds_pinnacle"] == {"1": 2.45, "X": 0.0, "2": 1.62}
    assert eng._marche_vivant(m)


def test_le_plafond_du_tier_2_se_repartit_entre_sports():
    """289 matchs chargés, 50 regardés, tous de foot (scan du 2026-09-29
    09:11) : le plafond doit laisser sa place à chaque sport."""
    import run_engine as eng
    items = ([{"sport": "soccer", "i": i} for i in range(80)]
             + [{"sport": "americanfootball", "i": i} for i in range(3)]
             + [{"sport": "hockey", "i": i} for i in range(10)])
    out = eng._repartir_par_sport(items, 50)
    assert len(out) == 50
    assert sum(1 for m in out if m["sport"] == "americanfootball") == 3
    assert sum(1 for m in out if m["sport"] == "hockey") == 10
    assert [m["i"] for m in out if m["sport"] == "soccer"] == list(range(37)), "ordre gardé"
    assert eng._repartir_par_sport(items[:5], 50) == items[:5]


def test_le_moteur_trie_sur_le_prix_sharp_avant_de_couper():
    moteur = (RACINE / "run_engine.py").read_text(encoding="utf-8")
    assert "for m in xbet_matches[:MAX_MATCHES]:" not in moteur
    t2 = moteur[moteur.index("xbet_matches = fetch_matches()"):]
    assert t2.index("_oddsapi_sharp_sans_execution()") < t2.index("_enrich_from_exchange(xbet_matches, exchange_prices")
    assert t2.index("_enrich_from_exchange(xbet_matches, exchange_prices") < t2.index("_repartir_par_sport(")


def test_un_sport_retire_ne_revient_pas_par_le_tier_2():
    """Baseball retiré « sport entier » le 2026-09-17 : odds-api.io le sert
    toujours, et seule la coupe à 50 le tenait loin de l'émission. Le filtre
    de périmètre doit précéder la sélection du Tier 2."""
    from core.odds_api import sports_au_perimetre
    assert "baseball" not in sports_au_perimetre()
    assert {"americanfootball", "tennis"} <= sports_au_perimetre()
    assert "college_football" not in sports_au_perimetre()   # retirée le 2026-10-06
    moteur = (RACINE / "run_engine.py").read_text(encoding="utf-8")
    t2 = moteur[moteur.index("xbet_matches = fetch_matches()"):]
    assert t2.index("not in perimetre") < t2.index("_repartir_par_sport(")


def test_un_sport_retire_ne_coute_plus_une_requete_odds_api_io(monkeypatch, _io):
    """Le compte de 400 req/j finance le football américain avec ce que le
    baseball retiré gaspillait (~24 req/j)."""
    touched = []
    monkeypatch.setattr(oai.requests, "get", lambda *a, **k: touched.append(a))
    assert oai.fetch_sport("baseball", api_key="k") == []
    assert touched == []
