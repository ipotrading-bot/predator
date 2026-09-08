"""
tests/test_incident_ucl_2026_09_08.py — soirée de Ligue des champions sans
signal : deux pertes silencieuses mesurées dans les logs des scans de 11:10
et 13:10 UTC le 2026-09-08.

1. « Real Madrid vs Inter Milano » (1xbet via odds-api.io) n'a jamais reçu de
   prix sharp : Pinnacle et Matchbook disent « Inter Milan », et le
   rapprochement refusait « Milano » contre « Milan » — « Échec prix Sharp »
   trois scans de suite sur l'affiche de la soirée.
2. L'appel OddsAPI de la ligue est mort sur un ConnectionResetError sans
   seconde tentative : la ligue entière a sauté pour ce scan.
"""
import pytest
import requests

import core.odds_api as odds_api
from core.exchange_match import lookup_exchange
from core.paim_engine import strict_team_match


# ── 1. Inter Milano ↔ Inter Milan ────────────────────────────────────────

@pytest.mark.parametrize("a, b", [
    ("Inter Milano", "Inter Milan"),
    ("Inter Milan", "Inter Milano"),
    ("Inter Milano", "Internazionale"),
    ("FC Internazionale Milano", "Inter Milan"),
])
def test_inter_milano_est_inter_milan(a, b):
    assert strict_team_match(a, b)


def test_inter_ne_devient_pas_un_joker():
    """Élargir l'alias ne doit pas apparier Inter avec n'importe quel « Inter »."""
    assert not strict_team_match("Inter Milano", "Inter Miami")
    # « Internacional » (Porto Alegre) reste apparié par le RATIO de
    # similarité avec « internazionale » (≥ 0,60) — limite ANTÉRIEURE à ce
    # correctif (« Inter Milan » y était déjà sujet), figée ici pour ne pas
    # être redécouverte en production ; ligues différentes, jamais en
    # concurrence dans un même slate.


def test_real_madrid_inter_milano_trouve_son_prix_matchbook():
    """Le cas exact du 2026-09-08 : le slate 1xbet contre les marchés Matchbook."""
    prix = {"real madrid_inter milan": {"home": "Real Madrid", "away": "Inter Milan",
                                        "1": 2.10, "X": 3.60, "2": 3.40,
                                        "_source": "matchbook"}}
    m = {"home": "Real Madrid", "away": "Inter Milano",
         "match": "Real Madrid vs Inter Milano"}
    hit = lookup_exchange(m, prix)
    assert hit is not None and hit["1"] == 2.10


# ── 2. Connection reset rejoué une fois ───────────────────────────────────

class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.headers = {"x-requests-remaining": "10", "x-requests-used": "1"}

    def json(self):
        return self._payload


def _event():
    return {"id": "ucl1", "sport_key": "soccer_uefa_champs_league",
            "commence_time": "2030-01-01T19:00:00Z",
            "home_team": "Real Madrid", "away_team": "Inter Milan",
            "bookmakers": [
                {"key": "pinnacle", "markets": [{"key": "h2h", "outcomes": [
                    {"name": "Real Madrid", "price": 2.0}, {"name": "Inter Milan", "price": 3.5},
                    {"name": "Draw", "price": 3.6}]}]},
                {"key": "onexbet", "markets": [{"key": "h2h", "outcomes": [
                    {"name": "Real Madrid", "price": 2.1}, {"name": "Inter Milan", "price": 3.4},
                    {"name": "Draw", "price": 3.5}]}]}]}


@pytest.fixture(autouse=True)
def _pool_propre(monkeypatch):
    monkeypatch.setattr(odds_api, "_RETRY_PAUSE_S", 0.0)
    monkeypatch.setattr(odds_api, "get_secret", lambda name, **kw: None)
    odds_api.reset_pool()
    yield
    odds_api.reset_pool()


def _wire(monkeypatch, resets_avant_succes: int):
    calls = {"odds": 0}

    def fake_get(url, params=None, timeout=None):
        if url.rstrip("/").endswith("/sports"):
            return _Resp([], status=200)
        if "/events" in url:
            return _Resp([{"id": "x", "commence_time": "2030-01-01T19:00:00Z"}])
        calls["odds"] += 1
        if calls["odds"] <= resets_avant_succes:
            raise requests.exceptions.ConnectionError(
                "('Connection aborted.', ConnectionResetError(104, 'Connection reset by peer'))")
        return _Resp([_event()])

    monkeypatch.setattr(odds_api.requests, "get", fake_get)
    return calls


def test_un_reset_est_rejoue_et_la_ligue_est_servie(monkeypatch):
    calls = _wire(monkeypatch, resets_avant_succes=1)
    events = odds_api.fetch_odds(api_key="k", hours_ahead=6,
                                 sport_keys={"soccer_uefa_champs_league": "soccer"})
    assert calls["odds"] == 2
    assert len(events) == 1


def test_deux_resets_de_suite_rendent_vide_sans_crasher(monkeypatch):
    """Politique « retour [] + log » conservée : une seule relance, pas une boucle."""
    calls = _wire(monkeypatch, resets_avant_succes=2)
    events = odds_api.fetch_odds(api_key="k", hours_ahead=6,
                                 sport_keys={"soccer_uefa_champs_league": "soccer"})
    assert calls["odds"] == 2
    assert events == []
