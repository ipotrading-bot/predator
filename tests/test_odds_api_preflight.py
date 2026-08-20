"""
tests/test_odds_api_preflight.py — le pré-vol gratuit de core/odds_api.py.

`/v4/sports/{key}/events` ne compte pas dans le quota, `/v4/sports/{key}/odds`
coûte [marchés] × [régions] (3 crédits pour un h2h,spreads,totals sur `eu`).
On payait ces 3 crédits pour chaque ligue de SPORT_KEYS à chaque scan, y
compris celles sans le moindre match dans la fenêtre — en Golden Hour
(fenêtre 2h) c'est le cas de presque toutes.

Épinglé ici : une ligue vide ne déclenche AUCUN appel payant, et une panne du
pré-vol ne doit pas être lue comme « pas de match » (ce serait une panne
silencieuse du pipeline entier).
"""
import core.odds_api as odds_api


class _Resp:
    def __init__(self, payload, status=200, remaining="400", used="100"):
        self._payload = payload
        self.status_code = status
        self.headers = {"x-requests-remaining": remaining, "x-requests-used": used}

    def json(self):
        return self._payload


def _wire(monkeypatch, events_by_key: dict, odds_payload=None):
    """Stubbe requests.get et journalise les URL appelées."""
    calls = {"events": [], "odds": []}

    def fake_get(url, params=None, timeout=None):
        if "/events" in url:
            key = url.split("/sports/")[1].split("/")[0]
            payload = events_by_key.get(key)
            if payload is None:
                return _Resp(None, status=500)      # pré-vol en panne
            calls["events"].append(key)
            return _Resp(payload)
        calls["odds"].append(url.split("/sports/")[1].split("/")[0])
        return _Resp(odds_payload if odds_payload is not None else [])

    monkeypatch.setattr(odds_api.requests, "get", fake_get)
    return calls


def test_empty_league_is_never_paid_for(monkeypatch):
    calls = _wire(monkeypatch, {"soccer_epl": [], "basketball_nba": [{"id": "e1"}]})
    odds_api.fetch_odds(api_key="k", hours_ahead=2,
                        sport_keys={"soccer_epl": "soccer", "basketball_nba": "basketball"})
    assert calls["odds"] == ["basketball_nba"]      # l'EPL n'a rien coûté


def test_preflight_failure_falls_through_to_the_paid_call(monkeypatch):
    # `None` = HTTP 500 sur le pré-vol : on ne sait pas, donc on laisse le
    # scan payant trancher plutôt que de rater une ligue entière.
    calls = _wire(monkeypatch, {"soccer_epl": None})
    odds_api.fetch_odds(api_key="k", hours_ahead=2, sport_keys={"soccer_epl": "soccer"})
    assert calls["odds"] == ["soccer_epl"]


def test_busiest_leagues_are_scanned_first(monkeypatch):
    # Si un 422 interrompt le scan, il doit l'interrompre sur les ligues les
    # moins fournies — pas au hasard de l'ordre du dictionnaire.
    calls = _wire(monkeypatch, {
        "soccer_epl":     [{"id": "a"}],
        "baseball_mlb":   [{"id": "b"}, {"id": "c"}, {"id": "d"}],
        "basketball_nba": [{"id": "e"}, {"id": "f"}],
    })
    odds_api.fetch_odds(api_key="k", hours_ahead=24, sport_keys={
        "soccer_epl": "soccer", "baseball_mlb": "baseball", "basketball_nba": "basketball"})
    assert calls["odds"] == ["baseball_mlb", "basketball_nba", "soccer_epl"]


def test_missing_key_returns_empty(monkeypatch):
    monkeypatch.delenv("ODDS_API_KEY", raising=False)
    assert odds_api.fetch_odds(api_key=None) == []


def test_no_local_guard_ever_stops_a_useful_scan(monkeypatch):
    """Décision opérateur du 2026-08-01 : « laisse OddsAPI couler ». Aucun
    garde local (ni l'ancien `remaining < 50`, ni un rationnement mensuel) ne
    doit interrompre un scan — seul un vrai 422 de l'API le peut."""
    calls = _wire(monkeypatch, {"a": [{"id": "1"}], "b": [{"id": "2"}]})

    def fake_get(url, params=None, timeout=None):
        if "/events" in url:
            calls["events"].append(url)
            return _Resp([{"id": "1"}])
        calls["odds"].append(url)
        return _Resp([], remaining="2", used="498")   # quota au ras du sol

    monkeypatch.setattr(odds_api.requests, "get", fake_get)
    odds_api.fetch_odds(api_key="k", hours_ahead=2,
                        sport_keys={"a": "soccer", "b": "basketball"})
    assert len(calls["odds"]) == 2      # les deux ligues scannées malgré remaining=2
