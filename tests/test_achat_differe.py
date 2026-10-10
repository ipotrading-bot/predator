"""
tests/test_achat_differe.py — une ligue sans book d'exécution n'est achetée
que pour les matchs qu'un book d'exécution cote ailleurs (2026-09-29).

MESURÉ sur les 35 scans standard du 23 au 29/09 : 56 achats de ligue sans un
seul match exploitable, jusqu'à ~168 crédits sur 643 (≈ 26 %) — NFL, NCAAF
(OddsAPI n'y cote jamais 1xbet), MMA, une partie du tennis. Le Pinnacle de
ces ligues ne sert qu'aux matchs qu'odds-api.io rend exécutables : on
l'achète donc APRÈS le Tier 2, par `eventIds`, et pas du tout s'il n'y en a
aucun. OddsAPI ne facture pas une réponse vide et `/events` est gratuit.

Ces gardiens tiennent : la mesure (qui décide du report), le report, l'achat
ciblé, le 0 crédit, la date des matchs, la panne du pré-vol, le coût réel.
"""
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import core.odds_api as odds_api
from core.scan_windows import SpendPolicy, motif_sans_execution, valeur_execution

RACINE = Path(__file__).resolve().parent.parent
NFL = {"americanfootball_nfl": "americanfootball"}
KICKOFF = "2030-01-01T00:15:00Z"


class _Resp:
    def __init__(self, payload, status=200, last=None):
        self._payload, self.status_code = payload, status
        self.headers = {"x-requests-remaining": "400", "x-requests-used": "100"}
        if last is not None:
            self.headers["x-requests-last"] = str(last)

    def json(self):
        return self._payload


def _nfl_sans_1xbet():
    return [{"id": "evt-browns", "home_team": "Cleveland Browns",
             "away_team": "Pittsburgh Steelers", "commence_time": KICKOFF,
             "bookmakers": [{"key": "pinnacle", "markets": [{"key": "h2h", "outcomes": [
                 {"name": "Cleveland Browns", "price": 2.45},
                 {"name": "Pittsburgh Steelers", "price": 1.62}]}]}]}]


@pytest.fixture
def api(monkeypatch):
    """OddsAPI simulé : /sports vide, /events = le pré-vol, /odds = la NFL
    sans 1xbet. Journalise chaque appel /odds et ses paramètres."""
    monkeypatch.setattr(odds_api, "get_secret", lambda name, **kw: None)
    monkeypatch.setenv("ODDS_API_KEY", "k")
    odds_api.reset_pool()
    etat = {"odds": [], "prevol": [{"id": "evt-browns", "home_team": "Cleveland Browns",
                                    "away_team": "Pittsburgh Steelers",
                                    "commence_time": KICKOFF}], "prevol_status": 200}

    def fake_get(url, params=None, timeout=None):
        if url.rstrip("/").endswith("/sports"):
            return _Resp([])
        if "/events" in url:
            return _Resp(etat["prevol"], status=etat["prevol_status"])
        etat["odds"].append(dict(params or {}))
        return _Resp(_nfl_sans_1xbet(), last=1)

    monkeypatch.setattr(odds_api.requests, "get", fake_get)
    yield etat
    odds_api.reset_pool()


def _politique(mesures: dict):
    """SpendPolicy sans rythme, dont la mémoire de couverture est `mesures`."""
    return SpendPolicy(lambda k: None, lambda k: None,
                       sans_execution=lambda k: motif_sans_execution(mesures.get(k)),
                       noter_execution=lambda k, a, n: mesures.__setitem__(k, valeur_execution(a, n)))


class TestLaMesure:
    def test_zero_sur_n_recent_diffère(self):
        now = datetime(2026, 9, 29, 12, tzinfo=timezone.utc)
        assert motif_sans_execution(valeur_execution(0, 16, now - timedelta(hours=5)), now) \
            == "0/16 il y a 5 h"

    @pytest.mark.parametrize("valeur", [
        "3/16|2026-09-29T07:00:00+00:00",     # des books d'exécution : achat normal
        "0/0|2026-09-29T07:00:00+00:00",      # rien reçu : rien mesuré
        "0/16|2026-09-20T07:00:00+00:00",     # plus d'une semaine : on re-mesure
        "0/16|2026-09-29T07:00:00",           # sans fuseau : illisible
        "n'importe quoi", "", None,
    ])
    def test_dans_le_doute_on_achete(self, valeur):
        now = datetime(2026, 9, 29, 12, tzinfo=timezone.utc)
        assert motif_sans_execution(valeur, now) is None

    def test_une_panne_de_la_memoire_n_empeche_jamais_l_achat(self):
        def boum(*_):
            raise RuntimeError("meta injoignable")
        pol = SpendPolicy(lambda k: None, lambda k: None,
                          sans_execution=boum, noter_execution=boum)
        assert pol.sharp_seul("americanfootball_nfl") is None
        pol.noter_execution("americanfootball_nfl", 0, 3)       # ne lève pas

    def test_chaque_achat_mesure_sa_couverture(self, api):
        mesures: dict = {}
        odds_api.fetch_odds(hours_ahead=24, sport_keys=NFL, spend_policy=_politique(mesures))
        assert len(api["odds"]) == 1
        assert mesures["americanfootball_nfl"].startswith("0/1|")


class TestLeReport:
    def test_une_ligue_mesuree_sans_execution_n_est_pas_achetee_au_tier1(self, api, caplog):
        mesures = {"americanfootball_nfl": valeur_execution(0, 16)}
        with caplog.at_level(logging.INFO, logger="PREDATOR.odds_api"):
            out = odds_api.fetch_odds(hours_ahead=24, sport_keys=NFL,
                                      spend_policy=_politique(mesures))
        assert out == [] and api["odds"] == []
        assert odds_api.DIFFERES == {"americanfootball_nfl": "americanfootball"}
        assert any("DIFFÉRÉ | americanfootball_nfl" in r.getMessage() for r in caplog.records)

    def test_pre_vol_en_panne_la_ligue_s_achete_comme_avant(self, api):
        """Une panne du pré-vol ne vaut jamais « pas de match »."""
        api["prevol_status"] = 500
        mesures = {"americanfootball_nfl": valeur_execution(0, 16)}
        odds_api.fetch_odds(hours_ahead=24, sport_keys=NFL, spend_policy=_politique(mesures))
        assert len(api["odds"]) == 1 and odds_api.DIFFERES == {}


class TestLAchatCible:
    def _differer(self, api):
        mesures = {"americanfootball_nfl": valeur_execution(0, 16)}
        pol = _politique(mesures)
        odds_api.fetch_odds(hours_ahead=24, sport_keys=NFL, spend_policy=pol)
        assert api["odds"] == []
        return pol

    def test_aucun_match_executable_ailleurs_zero_credit(self, api, caplog):
        pol = self._differer(api)
        with caplog.at_level(logging.INFO, logger="PREDATOR.odds_api"):
            gardes = odds_api.acheter_sharp_differe(
                [{"home": "Tulsa Golden Hurricane", "away": "North Texas Mean Green",
                  "commence_time": KICKOFF}], pol)
        assert gardes == 0 and api["odds"] == []
        assert any("0 crédit" in r.getMessage() for r in caplog.records)

    def test_achete_seulement_les_matchs_cotes_ailleurs(self, api):
        pol = self._differer(api)
        gardes = odds_api.acheter_sharp_differe(
            [{"home": "Cleveland Browns", "away": "Pittsburgh Steelers",
              "commence_time": "2030-01-01T00:15:00+00:00"}], pol)
        assert gardes == 1
        assert len(api["odds"]) == 1 and api["odds"][0]["eventIds"] == "evt-browns"
        row = odds_api.sharp_sans_execution()["cleveland browns_pittsburgh steelers"]
        assert (row["1"], row["2"], row["_source"]) == (2.45, 1.62, "pinnacle")

    def test_memes_equipes_autre_date_ce_n_est_pas_le_meme_match(self, api):
        pol = self._differer(api)
        assert odds_api.acheter_sharp_differe(
            [{"home": "Cleveland Browns", "away": "Pittsburgh Steelers",
              "commence_time": "2030-01-20T00:15:00+00:00"}], pol) == 0
        assert api["odds"] == []

    def test_match_ecrit_dans_l_autre_sens_achete_sans_lever(self, api):
        """2026-10-09 : odds-api.io écrit l'extérieur d'abord (NHL), l'index
        sans prix faisait lever KeyError('2') — trois scans standard tombés."""
        pol = self._differer(api)
        gardes = odds_api.acheter_sharp_differe(
            [{"home": "Pittsburgh Steelers", "away": "Cleveland Browns",
              "commence_time": "2030-01-01T00:15:00+00:00"}], pol)
        assert gardes == 1
        assert len(api["odds"]) == 1 and api["odds"][0]["eventIds"] == "evt-browns"

    def test_match_inverse_autre_date_ce_n_est_pas_le_meme_match(self, api):
        """Le retournement ne doit pas perdre le coup d'envoi."""
        pol = self._differer(api)
        assert odds_api.acheter_sharp_differe(
            [{"home": "Pittsburgh Steelers", "away": "Cleveland Browns",
              "commence_time": "2030-01-20T00:15:00+00:00"}], pol) == 0
        assert api["odds"] == []

    def test_sans_report_rien_ne_part(self, api):
        odds_api.DIFFERES.clear()
        assert odds_api.acheter_sharp_differe(
            [{"home": "Cleveland Browns", "away": "Pittsburgh Steelers"}], None) == 0
        assert api["odds"] == []


class TestLeCoutReel:
    def test_l_en_tete_fait_foi(self):
        assert odds_api.cout_reel(_Resp([], last=2), "americanfootball") == 2.0
        assert odds_api.cout_reel(_Resp([], last=0), "americanfootball") == 0.0

    def test_sans_en_tete_le_tarif_theorique(self):
        assert odds_api.cout_reel(_Resp([]), "americanfootball") == \
            odds_api.league_cost("americanfootball")


class TestLeMoteur:
    SRC = (RACINE / "run_engine.py").read_text(encoding="utf-8")

    def test_l_achat_differe_passe_apres_le_tier2_et_avant_la_pose_du_sharp(self):
        achat = self.SRC.index("_oddsapi_acheter_sharp_differe(xbet_matches, spend_policy)")
        pose = self.SRC.index("sharp_payes = _oddsapi_sharp_sans_execution()")
        assert achat < pose

    def test_la_politique_porte_la_memoire_de_couverture(self):
        corps = self.SRC.split("def _build_spend_policy(", 1)[1].split("\ndef ", 1)[0]
        assert "sans_execution=_sans_execution" in corps
        assert "noter_execution=_noter_execution" in corps
        assert 'f"oddsapi_exec_{sport_key}"' in corps


class TestCeQueMesureLaCouverture:
    """Le 29/09, 1xbet apparaissait sur Browns–Steelers chez OddsAPI sans y
    coter le 1X2 : la PRÉSENCE d'un book d'exécution n'est pas un match
    exploitable. La mesure compte les matchs exploitables."""

    def test_un_book_present_sans_1x2_ne_compte_pas(self, api, monkeypatch):
        ev = _nfl_sans_1xbet()[0]
        ev["bookmakers"].append({"key": odds_api.XBET_KEY, "markets": [
            {"key": "totals", "outcomes": [{"name": "Over", "price": 1.9, "point": 40.5},
                                           {"name": "Under", "price": 1.9, "point": 40.5}]}]})
        orig = odds_api.requests.get

        def fake_get(url, params=None, timeout=None):
            if "/odds" in url:
                api["odds"].append(dict(params or {}))
                return _Resp([ev], last=3)
            return orig(url, params=params, timeout=timeout)

        monkeypatch.setattr(odds_api.requests, "get", fake_get)
        mesures: dict = {}
        odds_api.fetch_odds(hours_ahead=24, sport_keys=NFL, spend_policy=_politique(mesures))
        assert mesures["americanfootball_nfl"].startswith("0/1|")

    def test_l_achat_cible_garde_le_sharp_meme_d_un_match_exploitable(self, api, monkeypatch):
        ev = _nfl_sans_1xbet()[0]
        ev["bookmakers"].append({"key": odds_api.XBET_KEY, "markets": [{"key": "h2h", "outcomes": [
            {"name": "Cleveland Browns", "price": 2.50},
            {"name": "Pittsburgh Steelers", "price": 1.58}]}]})
        mesures = {"americanfootball_nfl": valeur_execution(0, 16)}
        pol = _politique(mesures)
        odds_api.fetch_odds(hours_ahead=24, sport_keys=NFL, spend_policy=pol)
        orig = odds_api.requests.get

        def fake_get(url, params=None, timeout=None):
            if "/odds" in url:
                api["odds"].append(dict(params or {}))
                return _Resp([ev], last=3)
            return orig(url, params=params, timeout=timeout)

        monkeypatch.setattr(odds_api.requests, "get", fake_get)
        assert odds_api.acheter_sharp_differe(
            [{"home": "Cleveland Browns", "away": "Pittsburgh Steelers",
              "commence_time": KICKOFF}], pol) == 1
        # Et la mesure revient à « exploitable » : la ligue sera rachetée
        # normalement au Tier 1 la prochaine fois.
        assert mesures["americanfootball_nfl"].startswith("1/1|")
        assert motif_sans_execution(mesures["americanfootball_nfl"]) is None
