"""
tests/test_scan_windows.py — Phase 3 du recentrage : efficacité quota.

Trois règles de dépense OddsAPI, toutes loggées : pré-vol 0 crédit (déjà
testé dans test_odds_api_preflight.py), intervalle minimal hors fenêtre
favorable, garde de réserve — avec deux priorités absolues : les fenêtres
favorables et la capture de closing line ne sont JAMAIS espacées.
"""
from datetime import datetime, timezone

from core import odds_api
from core.scan_windows import (BACKGROUND_MIN_INTERVAL_MIN, RESERVE_CREDITS,
                               SpendPolicy, favorable_leagues, is_favorable)


def _utc(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


class TestWindowsMap:
    def test_kbo_morning_utc(self):
        assert is_favorable("baseball_kbo", _utc(2026, 8, 25, 8))        # mardi 08:00
        assert not is_favorable("baseball_kbo", _utc(2026, 8, 25, 15))

    def test_big5_european_evening(self):
        assert is_favorable("soccer_epl", _utc(2026, 8, 25, 19))
        assert not is_favorable("soccer_epl", _utc(2026, 8, 25, 4))

    def test_south_america_and_mlb_cross_midnight(self):
        assert is_favorable("soccer_brazil_campeonato", _utc(2026, 8, 25, 23))
        assert is_favorable("baseball_mlb", _utc(2026, 8, 26, 1))
        assert not is_favorable("baseball_mlb", _utc(2026, 8, 26, 10))

    def test_nfl_sunday_and_monday_night(self):
        assert is_favorable("americanfootball_nfl", _utc(2026, 9, 13, 18))   # dimanche
        assert is_favorable("americanfootball_nfl", _utc(2026, 9, 14, 1))    # lundi 01:00 (SNF)
        assert not is_favorable("americanfootball_nfl", _utc(2026, 9, 15, 18))  # mardi

    def test_euroleague_thursday_friday_only(self):
        assert is_favorable("basketball_euroleague", _utc(2026, 10, 1, 19))   # jeudi
        assert not is_favorable("basketball_euroleague", _utc(2026, 9, 28, 19))  # lundi

    def test_combat_sports_weekend(self):
        assert is_favorable("mma_mixed_martial_arts", _utc(2026, 8, 22, 3))   # samedi
        assert not is_favorable("boxing_boxing", _utc(2026, 8, 25, 3))        # mardi

    def test_unknown_league_is_never_favorable(self):
        assert not is_favorable("soccer_mars_league", _utc(2026, 8, 25, 19))

    def test_every_sport_key_has_a_window(self):
        # Une ligue sans fenêtre ne serait scannée qu'en fond : on veut une
        # décision explicite par ligue.
        from core.odds_api import SPORT_KEYS
        from core.scan_windows import _WINDOWS
        assert set(SPORT_KEYS) <= set(_WINDOWS), set(SPORT_KEYS) - set(_WINDOWS)

    def test_favorable_leagues_returns_a_set(self):
        assert isinstance(favorable_leagues(_utc(2026, 8, 25, 19)), set)


class _Policy:
    """SpendPolicy avec un état en mémoire pour les tests."""
    def __init__(self, ages=None, exempt=(), reserve=RESERVE_CREDITS):
        self.ages = dict(ages or {})
        self.noted = []
        self.p = SpendPolicy(lambda k: self.ages.get(k), self.noted.append,
                             exempt_sports=exempt, reserve_credits=reserve)


class TestSpendPolicy:
    BG_TIME = _utc(2026, 8, 25, 4)       # mardi 04:00 : EPL hors fenêtre

    def test_favorable_window_always_pays(self):
        pol = _Policy(ages={"soccer_epl": 10}).p
        ok, why = pol.allow("soccer_epl", "soccer", _utc(2026, 8, 25, 19), 5)
        assert ok and "favorable" in why

    def test_background_respects_min_interval(self):
        pol = _Policy(ages={"soccer_epl": BACKGROUND_MIN_INTERVAL_MIN - 1}).p
        ok, why = pol.allow("soccer_epl", "soccer", self.BG_TIME, 400)
        assert not ok and "déjà payé" in why
        assert pol.skipped and pol.skipped[0][0] == "soccer_epl"

    def test_background_pays_after_the_interval_or_when_never_paid(self):
        assert _Policy(ages={"soccer_epl": BACKGROUND_MIN_INTERVAL_MIN + 1}).p \
            .allow("soccer_epl", "soccer", self.BG_TIME, 400)[0]
        assert _Policy().p.allow("soccer_epl", "soccer", self.BG_TIME, 400)[0]

    def test_reserve_guard_spaces_background_only(self):
        low = RESERVE_CREDITS - 1
        assert not _Policy().p.allow("soccer_epl", "soccer", self.BG_TIME, low)[0]
        assert _Policy().p.allow("soccer_epl", "soccer", _utc(2026, 8, 25, 19), low)[0]
        assert _Policy(exempt={"soccer"}).p.allow("soccer_epl", "soccer", self.BG_TIME, low)[0]

    def test_closing_line_exemption_beats_the_interval(self):
        pol = _Policy(ages={"soccer_epl": 5}, exempt={"soccer"}).p
        ok, why = pol.allow("soccer_epl", "soccer", self.BG_TIME, 400)
        assert ok and "closing line" in why

    def test_unknown_remaining_never_triggers_the_reserve(self):
        assert _Policy().p.allow("soccer_epl", "soccer", self.BG_TIME, None)[0]


class _Resp:
    def __init__(self, payload, status=200, remaining="400"):
        self._payload, self.status_code = payload, status
        self.headers = {"x-requests-remaining": remaining, "x-requests-used": "100"}

    def json(self):
        return self._payload


def test_fetch_odds_honours_the_policy_and_notes_paid_leagues(monkeypatch):
    calls = {"odds": []}

    def fake_get(url, params=None, timeout=None):
        if url.rstrip("/").endswith("/sports"):
            return _Resp([])
        key = url.split("/sports/")[1].split("/")[0]
        if "/events" in url:
            return _Resp([{"id": "e1"}])          # les deux ligues sont peuplées
        calls["odds"].append(key)
        return _Resp([])

    monkeypatch.setattr(odds_api.requests, "get", fake_get)
    # Mardi 04:00 UTC : EPL hors fenêtre et payée il y a 30 min → sautée ;
    # baseball_kbo hors fenêtre mais jamais payée → payée et notée.
    monkeypatch.setattr(odds_api, "datetime", _FrozenDT)
    pol = _Policy(ages={"soccer_epl": 30})
    odds_api.fetch_odds(api_key="k", hours_ahead=24,
                        sport_keys={"soccer_epl": "soccer", "baseball_kbo": "baseball"},
                        spend_policy=pol.p)
    assert calls["odds"] == ["baseball_kbo"]
    assert pol.noted == ["baseball_kbo"]
    assert [k for k, _ in pol.p.skipped] == ["soccer_epl"]


def test_without_policy_fetch_odds_pays_like_before(monkeypatch):
    calls = {"odds": []}

    def fake_get(url, params=None, timeout=None):
        if url.rstrip("/").endswith("/sports"):
            return _Resp([])
        key = url.split("/sports/")[1].split("/")[0]
        if "/events" in url:
            return _Resp([{"id": "e1"}])
        calls["odds"].append(key)
        return _Resp([])

    monkeypatch.setattr(odds_api.requests, "get", fake_get)
    odds_api.fetch_odds(api_key="k", hours_ahead=24, sport_keys={"soccer_epl": "soccer"})
    assert calls["odds"] == ["soccer_epl"]


class _FrozenDT(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 8, 25, 4, 0, tzinfo=timezone.utc)


def test_pool_remaining_is_tracked_from_paid_responses(monkeypatch):
    def fake_get(url, params=None, timeout=None):
        if url.rstrip("/").endswith("/sports"):
            return _Resp([], remaining="321")
        if "/events" in url:
            return _Resp([{"id": "e1"}])
        return _Resp([], remaining="318")

    monkeypatch.setattr(odds_api.requests, "get", fake_get)
    odds_api.fetch_odds(api_key="k", hours_ahead=24, sport_keys={"soccer_epl": "soccer"})
    assert odds_api.pool_remaining() == 318


def test_sports_with_imminent_signals(monkeypatch):
    import run_engine as eng

    class _Q:
        def __init__(self, rows): self.rows = rows
        def select(self, *_a, **_k): return self
        def eq(self, *_a): return self
        def gte(self, *_a): return self
        def lte(self, *_a): return self
        def limit(self, *_a): return self
        def execute(self):
            return type("R", (), {"data": self.rows})()

    class _SB:
        def table(self, _n): return _Q([{"sport": "soccer"}, {"sport": "mma"}])

    got = eng._sports_with_imminent_signals(_SB(), datetime.now(timezone.utc))
    assert got == {"soccer", "mma"}
    assert eng._sports_with_imminent_signals(None, datetime.now(timezone.utc)) == set()
