"""
tests/test_tennis_odds_api_io.py — le tennis du circuit principal par
odds-api.io (2026-10-01).

OddsAPI vend l'ATP de Tokyo et de Pékin SANS book d'exécution (0/9 et 0/5
mesurés) ; odds-api.io, lui, y porte 1xbet et Bet365, totaux de jeux compris.
Trois défauts empilés l'empêchaient de servir — aucun signal de tennis n'était
jamais sorti de cette source :

  1. le calendrier (~225 matchs sur 30 h, à 90 % ITF, UTR et Challengers)
     était coupé aux 60 PREMIERS PAR HEURE : 46 matchs rendus, 0 exploitable,
     et les simples ATP, joués 13 h plus tard, jamais lus ;
  2. les joueurs y sont écrits « Munar, Jaume » : aucun rapprochement avec
     « Jaume Munar » (OddsAPI, Matchbook, Smarkets, ESPN) ;
  3. le total du match s'y appelle « Totals (Games) » ; seul « Totals »
     était lu.

Contrat gardé ici :
  · seuls les matchs appariés au pré-vol gratuit d'un tournoi RETENU se
    paient — le périmètre « 500 et plus » de l'opérateur, sans liste de villes ;
  · sans pré-vol (Tier 1 éteint), le comportement d'avant ;
  · les noms sortent prénom d'abord ; les doubles ne bougent pas ;
  · le total de jeux entre, pas les totaux d'aces ni de set.

Aucun réseau : `requests.get` est monkeypatché.
"""
import logging

import pytest

import core.odds_api as oa
import core.odds_api_io as oai


class _Resp:
    def __init__(self, payload, status=200):
        self._payload, self.status_code, self.text = payload, status, "body"

    def json(self):
        return self._payload


def _event(eid, home, away, league, date="2030-01-02T03:00:00Z"):
    return {"id": eid, "home": home, "away": away, "status": "pending", "date": date,
            "league": {"name": league}, "sport": {"slug": "tennis"}}


def _books(ml=(1.62, 2.29)):
    marches = [
        {"name": "ML", "odds": [{"home": str(ml[0]), "away": str(ml[1])}]},
        {"name": "Totals (Games)", "odds": [{"hdp": 20, "over": "1.34", "under": "2.81"},
                                            {"hdp": 22.5, "over": "1.90", "under": "1.86"},
                                            {"hdp": 24, "over": "2.60", "under": "1.42"}]},
        {"name": "Spread (Games)", "odds": [{"hdp": -3.5, "home": "1.90", "away": "1.86"}]},
        {"name": "Totals (Aces)", "odds": [{"hdp": 9.5, "over": "1.85", "under": "1.85"}]},
        {"name": "Totals 1st Set", "odds": [{"hdp": 9.5, "over": "1.80", "under": "1.90"}]},
    ]
    return {"1xbet": marches}


def _wire(monkeypatch, events, cotes):
    calls = {"multi": []}

    def fake_get(url, timeout=None, params=None):
        if url.endswith("/bookmakers/selected"):
            return _Resp({"bookmakers": ["1xbet"], "count": 1})
        if url.endswith("/events"):
            return _Resp(events)
        if url.endswith("/odds/multi"):
            ids = params["eventIds"].split(",")
            calls["multi"].append(ids)
            return _Resp([dict(e, bookmakers=cotes) for e in events if str(e["id"]) in ids])
        raise AssertionError(url)
    monkeypatch.setattr(oai.requests, "get", fake_get)
    oai.reset_cache()
    return calls


@pytest.fixture(autouse=True)
def _propre(monkeypatch):
    monkeypatch.delenv("ODDS_API_IO_BOOKMAKERS", raising=False)
    monkeypatch.setattr(oai, "get_secret", lambda name, **kw: None)
    monkeypatch.setattr(oa, "_PREVOL_EVENTS", {})
    oai.reset_cache()
    yield
    oai.reset_cache()


def _prevol(monkeypatch, par_cle):
    monkeypatch.setattr(oa, "_PREVOL_EVENTS", {
        cle: [{"id": f"{cle}{i}", "home_team": h, "away_team": a, "commence_time": t}
              for i, (h, a, t) in enumerate(matchs)]
        for cle, matchs in par_cle.items()})


# ── Les noms ─────────────────────────────────────────────────────────────

class TestLePrenomDAbord:
    @pytest.mark.parametrize("brut,attendu", [
        ("Munar, Jaume", "Jaume Munar"),
        ("Carreno Busta, Pablo", "Pablo Carreno Busta"),
        ("Ruse, Elena-Gabriela", "Elena-Gabriela Ruse"),
        ("  Fils,  Arthur ", "Arthur Fils"),
    ])
    def test_nom_virgule_prenom_est_retourne(self, brut, attendu):
        assert oai.nom_tennis(brut) == attendu

    @pytest.mark.parametrize("brut", [
        "Max Sheldon",                              # ITF : déjà dans l'ordre
        "Galloway R / Goransson A",                 # double
        "Roncadelli, F / Villanueva, G",            # double avec virgules
        "Smith, John, Jr",                          # deux virgules : on ne devine pas
        "Munar,", ",Jaume", "",
    ])
    def test_le_reste_ne_bouge_pas(self, brut):
        assert oai.nom_tennis(brut) == brut.strip()

    def test_le_match_rendu_au_moteur_porte_les_noms_retournes(self):
        m = oai._to_match(dict(_event(1, "Vacherot, Valentin", "Tsitsipas, Stefanos",
                                      "ATP - Tokyo, Japan"), bookmakers=_books()),
                          "tennis", 3, False)
        assert m["match"] == "Valentin Vacherot vs Stefanos Tsitsipas"
        assert (m["home"], m["away"]) == ("Valentin Vacherot", "Stefanos Tsitsipas")

    def test_les_autres_sports_gardent_leurs_noms(self):
        """Une virgule dans un nom d'équipe n'est pas un prénom."""
        m = oai._to_match(dict(_event(1, "Juventud, Las Piedras", "Racing", "L"),
                               bookmakers=_books()), "soccer", 1, True)
        assert m["home"] == "Juventud, Las Piedras"


# ── Le total de jeux ─────────────────────────────────────────────────────

class TestLeTotalDeJeux:
    def test_totals_games_entre_comme_un_total(self):
        m = oai._to_match(dict(_event(1, "A, B", "C, D", "ATP - Tokyo, Japan"),
                               bookmakers=_books()), "tennis", 3, False)
        tot = m["totals_1xbet"]
        assert tot["point"] == 22.5 and (tot["over"], tot["under"]) == (1.90, 1.86)
        assert sorted(r["point"] for r in tot["ladder"]) == [20.0, 22.5, 24.0]
        assert tot["books"] == {"over": "1xbet", "under": "1xbet"}

    def test_ni_les_aces_ni_le_premier_set_ni_le_handicap_de_jeux(self):
        """Un « total » d'aces à 9,5 comparé au total de jeux du sharp serait
        deux paris différents ; le handicap de jeux n'est pas un marché que le
        Tier 1 sert au tennis (`_MARKETS_BY_SPORT` : h2h,totals)."""
        m = oai._to_match(dict(_event(1, "A, B", "C, D", "ATP - Tokyo, Japan"),
                               bookmakers=_books()), "tennis", 3, False)
        assert 9.5 not in [r["point"] for r in m["totals_1xbet"]["ladder"]]
        assert "spreads_1xbet" not in m
        assert oa._MARKETS_BY_SPORT["tennis"] == "h2h,totals"


# ── Le périmètre : tournois retenus seulement ────────────────────────────

CALENDRIER = [
    _event(1, "Sheldon, Max", "Pieczkowski, Olaf", "Tennis - ITF Men Ann Arbor - R16",
           "2030-01-01T16:00:00Z"),
    _event(2, "Nakashima, Bryce", "Smith, Keegan", "Challenger - Columbus, USA",
           "2030-01-01T17:00:00Z"),
    _event(3, "Galloway R / Goransson A", "Tabilo A / van Assche L",
           "ATP - Tokyo, Japan, Doubles", "2030-01-02T02:00:00Z"),
    _event(4, "Munar, Jaume", "Faria, Jaime", "ATP - Tokyo, Japan", "2030-01-02T02:00:00Z"),
    _event(5, "Medvedev, Daniil", "Carreno Busta, Pablo", "ATP - Beijing, China",
           "2030-01-02T07:00:00Z"),
    _event(6, "Lehecka, Jiri", "Humbert, Ugo", "ATP - Almaty, Kazakhstan",
           "2030-01-02T08:00:00Z"),                                   # un 250
]
PREVOL = {
    "tennis_atp_japan_open": [("Jaume Munar", "Jaime Faria", "2030-01-02T02:00:00Z")],
    "tennis_atp_china_open": [("Daniil Medvedev", "Pablo Carreno Busta",
                               "2030-01-02T07:30:00Z")],
}


class TestSeulsLesTournoisRetenusSePaient:
    def test_le_prevol_designe_les_matchs_lus(self, monkeypatch, caplog):
        _prevol(monkeypatch, PREVOL)
        calls = _wire(monkeypatch, CALENDRIER, _books())
        with caplog.at_level(logging.INFO):
            out = oai.fetch_sport("tennis", api_key="k", hours_ahead=24 * 3650)
        assert calls["multi"] == [["4", "5"]], "ni ITF, ni Challenger, ni double, ni 250"
        assert sorted(m["match"] for m in out) == [
            "Daniil Medvedev vs Pablo Carreno Busta", "Jaume Munar vs Jaime Faria"]
        assert any("2 match(s) des tournois retenus, 4 écarté(s)" in r.getMessage()
                   for r in caplog.records)

    def test_sans_tournoi_retenu_au_prevol_on_lit_comme_avant(self, monkeypatch, caplog):
        """Tier 1 éteint ou pool mort : une panne n'est jamais « pas de match »."""
        calls = _wire(monkeypatch, CALENDRIER, _books())
        with caplog.at_level(logging.INFO):
            oai.fetch_sport("tennis", api_key="k", hours_ahead=24 * 3650)
        assert sorted(calls["multi"][0]) == ["1", "2", "3", "4", "5", "6"]
        assert any("pré-vol OddsAPI sans tournoi retenu" in r.getMessage()
                   for r in caplog.records)

    def test_des_tournois_retenus_sans_match_ne_paient_rien(self, monkeypatch):
        _prevol(monkeypatch, {"tennis_atp_japan_open": []})
        calls = _wire(monkeypatch, CALENDRIER, _books())
        assert oai.fetch_sport("tennis", api_key="k", hours_ahead=24 * 3650) == []
        assert calls["multi"] == []

    def test_un_homonyme_a_plus_de_douze_heures_n_est_pas_le_meme_match(self, monkeypatch):
        _prevol(monkeypatch, {"tennis_atp_japan_open": [
            ("Jaume Munar", "Jaime Faria", "2030-01-03T02:00:00Z")]})   # le lendemain
        calls = _wire(monkeypatch, CALENDRIER, _books())
        assert oai.fetch_sport("tennis", api_key="k", hours_ahead=24 * 3650) == []
        assert calls["multi"] == []

    def test_un_seul_joueur_apparie_ne_suffit_pas(self, monkeypatch):
        _prevol(monkeypatch, {"tennis_atp_japan_open": [
            ("Jaume Munar", "Stefanos Tsitsipas", "2030-01-02T02:00:00Z")]})
        calls = _wire(monkeypatch, CALENDRIER, _books())
        assert oai.fetch_sport("tennis", api_key="k", hours_ahead=24 * 3650) == []
        assert calls["multi"] == []

    def test_les_autres_sports_ne_passent_pas_par_ce_filtre(self, monkeypatch):
        _prevol(monkeypatch, PREVOL)
        ev = [dict(_event(9, "A FC", "B FC", "Ligue Test"), sport={"slug": "football"})]
        calls = _wire(monkeypatch, ev, {"1xbet": [
            {"name": "ML", "odds": [{"home": "2.0", "draw": "3.4", "away": "3.6"}]}]})
        assert len(oai.fetch_sport("soccer", api_key="k", hours_ahead=24 * 3650)) == 1
        assert calls["multi"] == [["9"]]


class TestLIndexDuPrevol:
    def test_none_sans_cle_tennis(self, monkeypatch):
        monkeypatch.setattr(oa, "_PREVOL_EVENTS", {"soccer_epl": [
            {"id": "1", "home_team": "A", "away_team": "B", "commence_time": "t"}]})
        assert oa.matchs_prevol_tennis() is None

    def test_les_tournois_atp_et_wta_sont_reunis(self, monkeypatch):
        _prevol(monkeypatch, {**PREVOL, "tennis_wta_china_open": [
            ("Janice Tjen", "Diana Shnaider", "2030-01-02T03:00:00Z")]})
        index = oa.matchs_prevol_tennis()
        assert set(index) == {"jaume munar_jaime faria", "janice tjen_diana shnaider",
                              "daniil medvedev_pablo carreno busta"}
        assert index["jaume munar_jaime faria"]["commence_time"] == "2030-01-02T02:00:00Z"

    def test_des_tournois_sans_match_rendent_un_index_vide_pas_none(self, monkeypatch):
        _prevol(monkeypatch, {"tennis_atp_japan_open": []})
        assert oa.matchs_prevol_tennis() == {}
