"""
tests/test_mma_boxing_oddsapi.py — Phase 1 du recentrage sports (2026-08-22).

Le MMA quitte la recherche web (fetch_mma_events, supprimé) pour le flux
OddsAPI réel ; la boxe arrive par le même flux. h2h uniquement, prix
Pinnacle réel, capture de closing line couverte — c'est ce qui rend enfin
l'edge MMA validable par CLV réel. Les semaines sans carte ne coûtent rien :
le pré-vol gratuit rend 0 et la ligue n'est jamais payée.
"""
import run_engine
from core import harvester, odds_api
from core.closing_line import closing_price_for
from core.constants import KELLY_FRACTION
from core.learning_layer import SPORT_DEFAULTS
from core.odds_api import SPORT_KEYS, _MARKETS_BY_SPORT


class TestWiring:
    def test_sport_keys_map_mma_and_boxing(self):
        assert SPORT_KEYS["mma_mixed_martial_arts"] == "mma"
        assert SPORT_KEYS["boxing_boxing"] == "boxing"

    def test_h2h_only_for_combat_sports(self):
        assert _MARKETS_BY_SPORT["mma"] == "h2h"
        assert _MARKETS_BY_SPORT["boxing"] == "h2h"

    def test_kelly_fractions_reflect_a_real_sharp_price(self):
        assert KELLY_FRACTION["mma"] == 0.10       # était 0.08 (prix web)
        assert KELLY_FRACTION["boxing"] == 0.08    # marché mince, non validé
        # ...mais toujours SOUS les sports majeurs tant que le ledger n'a pas tranché.
        assert KELLY_FRACTION["mma"] < KELLY_FRACTION["basketball"]

    def test_learning_layer_knows_both(self):
        assert "mma" in SPORT_DEFAULTS and "boxing" in SPORT_DEFAULTS

    def test_web_search_mma_fetcher_is_gone(self):
        assert not hasattr(harvester, "fetch_mma_events")
        assert not hasattr(run_engine, "fetch_mma_events")
        # _NO_ODDSAPI_SPORTS a disparu le 2026-09-02 avec la file oracle
        # qu'il priorisait.
        assert not hasattr(run_engine, "_NO_ODDSAPI_SPORTS")

    def test_portfolio_and_golden_hour_cover_them(self):
        assert "mma" in run_engine._QUOTA_FAST and "boxing" in run_engine._QUOTA_FAST
        assert "mma_mixed_martial_arts" in run_engine.GOLDEN_SPORT_KEYS
        assert "boxing_boxing" in run_engine.GOLDEN_SPORT_KEYS


class _Resp:
    def __init__(self, payload, status=200):
        self._payload, self.status_code = payload, status
        self.headers = {"x-requests-remaining": "400", "x-requests-used": "100"}

    def json(self):
        return self._payload


def test_empty_fight_card_costs_zero_credits(monkeypatch):
    calls = {"events": [], "odds": []}

    def fake_get(url, params=None, timeout=None):
        if url.rstrip("/").endswith("/sports"):
            return _Resp([])
        key = url.split("/sports/")[1].split("/")[0]
        if "/events" in url:
            calls["events"].append(key)
            return _Resp([])                 # aucune carte dans la fenêtre
        calls["odds"].append(key)
        return _Resp([])

    monkeypatch.setattr(odds_api.requests, "get", fake_get)
    odds_api.fetch_odds(api_key="k", hours_ahead=24,
                        sport_keys={"mma_mixed_martial_arts": "mma",
                                    "boxing_boxing": "boxing"})
    assert set(calls["events"]) == {"mma_mixed_martial_arts", "boxing_boxing"}
    assert calls["odds"] == [], "semaine sans combat : pas un crédit dépensé"


def test_closing_line_prices_an_mma_h2h_signal():
    sig = {"sport": "mma", "market_key": "h2h", "match": "Jones vs Aspinall",
           "selection_name": "Aspinall"}
    event = {"home": "Jones", "away": "Aspinall", "sport": "mma",
             "odds_pinnacle": {"1": 2.30, "X": 0.0, "2": 1.65}}
    price = closing_price_for(sig, event)
    assert price is not None and abs(price - 1.65) < 1e-9
