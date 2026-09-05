"""
tests/test_scan_windows.py — Phase 3 du recentrage : efficacité quota.

Trois règles de dépense OddsAPI, toutes loggées : pré-vol 0 crédit (déjà
testé dans test_odds_api_preflight.py), intervalle minimal hors fenêtre
favorable, garde de réserve — avec deux priorités absolues : les fenêtres
favorables et la capture de closing line ne sont JAMAIS espacées.
"""
import pytest
from datetime import datetime, timedelta, timezone

from core import odds_api
from core.scan_windows import (BACKGROUND_MIN_INTERVAL_MIN, RESERVE_CREDITS,
                               SpendPolicy, favorable_leagues, is_favorable)


def _utc(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


class TestWindowsMap:
    """Carte recalée le 2026-09-03 : une fenêtre = l'heure d'un SCAN standard
    (06/09/11/13/16/19/21/23) situé T-2h30..6h AVANT les coups d'envoi de
    la ligue — un scan à moins de 2 h n'achetait que des fantômes."""

    def test_kbo_scan_du_matin_seulement(self):
        assert is_favorable("baseball_kbo", _utc(2026, 8, 25, 6))         # mardi 06:03
        assert not is_favorable("baseball_kbo", _utc(2026, 8, 25, 9))     # 09:03 : T-30 min
        assert not is_favorable("baseball_kbo", _utc(2026, 8, 25, 15))

    def test_big5_paye_avant_la_soiree_jamais_pendant(self):
        # Coups d'envoi 16:45–19:30 UTC : 13:03 et 16:03 oui, 19:03 non.
        assert is_favorable("soccer_epl", _utc(2026, 8, 25, 13))
        assert is_favorable("soccer_epl", _utc(2026, 8, 25, 16))
        assert not is_favorable("soccer_epl", _utc(2026, 8, 25, 19))
        assert not is_favorable("soccer_epl", _utc(2026, 8, 25, 4))

    def test_big5_week_end_des_le_matin(self):
        assert is_favorable("soccer_epl", _utc(2026, 8, 29, 9))            # samedi 09:03
        assert not is_favorable("soccer_epl", _utc(2026, 8, 25, 9))        # mardi 09:03

    def test_south_america_and_mlb_evening(self):
        assert is_favorable("soccer_brazil_campeonato", _utc(2026, 8, 25, 19))
        assert is_favorable("soccer_brazil_campeonato", _utc(2026, 8, 25, 23))
        assert is_favorable("baseball_mlb", _utc(2026, 8, 25, 21))
        assert is_favorable("baseball_mlb", _utc(2026, 8, 25, 13))         # matinées US
        assert not is_favorable("baseball_mlb", _utc(2026, 8, 26, 1))      # 01:00 : matchs commencés
        assert not is_favorable("baseball_mlb", _utc(2026, 8, 26, 10))

    def test_nfl_sunday_and_monday_night(self):
        assert is_favorable("americanfootball_nfl", _utc(2026, 9, 13, 13))   # dimanche 13:03 (17:00)
        assert is_favorable("americanfootball_nfl", _utc(2026, 9, 13, 21))   # dimanche 21:03 (SNF 00:20)
        assert is_favorable("americanfootball_nfl", _utc(2026, 9, 14, 21))   # lundi 21:03 (MNF)
        assert not is_favorable("americanfootball_nfl", _utc(2026, 9, 15, 18))  # mardi

    def test_euroleague_thursday_friday_only(self):
        assert is_favorable("basketball_euroleague", _utc(2026, 10, 1, 13))   # jeudi 13:03
        assert not is_favorable("basketball_euroleague", _utc(2026, 10, 1, 19))
        assert not is_favorable("basketball_euroleague", _utc(2026, 9, 28, 13))  # lundi

    def test_combat_sports_weekend(self):
        assert is_favorable("mma_mixed_martial_arts", _utc(2026, 8, 22, 21))   # samedi 21:03
        assert not is_favorable("boxing_boxing", _utc(2026, 8, 25, 21))        # mardi

    def test_australie_veille_et_matin(self):
        assert is_favorable("aussierules_afl", _utc(2026, 8, 28, 23))          # vendredi 23:03
        assert is_favorable("rugbyleague_nrl", _utc(2026, 8, 29, 6))           # samedi 06:03
        assert not is_favorable("aussierules_afl", _utc(2026, 8, 29, 9))

    def test_aucune_fenetre_a_moins_de_2h_dun_cron_standard_inutile(self):
        # Chaque fenêtre contient au moins une heure de scan standard —
        # sinon la ligue ne serait jamais payée au rang « fenêtre ».
        from scripts.ci_scan_mode import CRON_MODES
        from core.scan_windows import _WINDOWS
        hours = {int(h) for c, m in CRON_MODES.items() if m == "standard"
                 for h in c.split()[1].split(",")}
        for key, windows in _WINDOWS.items():
            for _days, start, end in windows:
                assert any(start <= h < end for h in hours), (key, start, end)

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
        ok, why = pol.allow("soccer_epl", "soccer", _utc(2026, 8, 25, 13), 5)
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
        assert _Policy().p.allow("soccer_epl", "soccer", _utc(2026, 8, 25, 13), low)[0]
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


# ── Rythme mensuel (2026-09-01) ───────────────────────────────────────
# « 1 mois seulement, maximum d'utilisation, suffisant pour tenir 30 jours » :
# le pool entier doit être dépensé sur le cycle, jamais avant sa fin.

from core.scan_windows import daily_allowance, intraday_cap, BACKGROUND_SHARE, EXEMPT_SHARE  # noqa: E402


class TestRythme:
    NIGHT = _utc(2026, 9, 2, 1)        # 01:00 UTC, mercredi : SA/MLB favorables, EPL de fond
    EVENING = _utc(2026, 9, 2, 22)     # 22:00 UTC : plafond intra-journée = 100 %

    def test_allocation_du_jour_est_pool_sur_jours_restants(self):
        assert daily_allowance(2500, 30) == pytest.approx(2500 / 30)
        assert daily_allowance(2500, 0.2) == 2500          # dernier jour : tout
        assert daily_allowance(None, 30) is None
        assert daily_allowance(2500, None) is None

    def test_plafond_intra_journee_monte_lineairement(self):
        assert intraday_cap(240, _utc(2026, 9, 2, 0)) == pytest.approx(240 * 2 / 24)
        assert intraday_cap(240, _utc(2026, 9, 2, 10)) == pytest.approx(240 * 12 / 24)
        assert intraday_cap(240, self.EVENING) == 240
        assert intraday_cap(240, _utc(2026, 9, 2, 23, 30)) == 240

    def _pol(self, allowance, spent=0.0, exempt=()):
        noted = []
        p = SpendPolicy(lambda _k: None, lambda _k: None, exempt_sports=exempt,
                        allowance=allowance, spent_today=spent,
                        note_spent=noted.append)
        return p, noted

    def test_fenetre_favorable_bornee_par_le_plafond_du_jour(self):
        dawn = _utc(2026, 9, 2, 5)                   # 05:00 : KBO/NPB en fenêtre
        p, _ = self._pol(allowance=12, spent=0)     # plafond 12 × 7/24 = 3,5
        ok, why = p.allow("baseball_kbo", "baseball", dawn, 2000, cost=2)
        assert ok and why == "fenêtre favorable"
        ok, why = p.allow("baseball_npb", "baseball", dawn, 2000, cost=3)
        assert not ok and "rythme" in why
        assert p.skipped and "fenêtre favorable mais" in p.skipped[0][1]

    def test_le_soir_le_plafond_est_l_allocation_entiere(self):
        late = _utc(2026, 9, 2, 21, 30)              # SA en fenêtre, plafond 24 × 23,5/24 = 23,5
        p, _ = self._pol(allowance=24, spent=15)
        sa = "soccer_brazil_campeonato"
        assert p.allow(sa, "soccer", late, 2000, cost=3)[0]      # 18
        assert p.allow(sa, "soccer", late, 2000, cost=3)[0]      # 21
        assert not p.allow(sa, "soccer", late, 2000, cost=3)[0]  # 24 > 23,5
        assert p.engaged == 6                        # comptés dans CE scan, avant paiement

    def test_le_fond_ne_prend_que_sa_part(self):
        # 22:00 : plafond = allocation ; le fond s'arrête à BACKGROUND_SHARE
        p, _ = self._pol(allowance=10, spent=10 * BACKGROUND_SHARE - 1)
        # EPL à 22:00 est hors fenêtre (17-22 exclu) → scan de fond
        assert p.allow("soccer_epl", "soccer", self.EVENING, 2000, cost=1)[0]
        ok, why = p.allow("soccer_epl", "soccer", self.EVENING, 2000, cost=1)
        assert not ok and "scan de fond" in p.skipped[0][1]
        # …mais une ligue en fenêtre passe encore (NBA 22-04)
        assert p.allow("basketball_nba", "basketball", self.EVENING, 2000, cost=3)[0]

    def test_closing_line_imminente_reste_prioritaire_mais_suit_l_heure(self):
        """Elle IGNORAIT le plafond horaire (`intraday=False`) jusqu'au
        2026-09-05, et c'est par là que la nuit mangeait la soirée. Elle garde
        la part la plus haute — au-dessus de la fenêtre favorable et du fond —
        mais elle n'est plus hors du temps."""
        # 01:00 : plafond horaire = 10 × 3/24 × 1,1 = 1,375 — le débordement
        # d'antan (11 ≤ 11) n'a plus lieu.
        p, _ = self._pol(allowance=10, spent=10, exempt={"soccer"})
        ok, _ = p.allow("soccer_epl", "soccer", self.NIGHT, 2000, cost=1)
        assert not ok and "closing line imminente mais" in p.skipped[0][1]
        # 22:00 : la journée est écoulée, elle déborde encore jusqu'à 1,1×.
        p, _ = self._pol(allowance=10, spent=10, exempt={"soccer"})
        assert p.allow("soccer_epl", "soccer", self.EVENING, 2000, cost=1)[0]   # 11 ≤ 11
        ok, _ = p.allow("soccer_epl", "soccer", self.EVENING, 2000, cost=1)
        assert not ok
        assert EXEMPT_SHARE == pytest.approx(1.1)
        assert EXEMPT_SHARE > 1.0 > BACKGROUND_SHARE

    def test_le_tick_de_nuit_ne_peut_plus_manger_la_soiree(self):
        """Rejeu du 2026-09-05 : à 00:40, allocation 117, la voie exemptée a
        laissé passer 37 crédits pour un plafond horaire de ~13 ; les créneaux
        de 16:03 et 19:03 ont ensuite acheté 0 cote et la soirée n'a pas sorti
        un signal recommandé."""
        nuit = _utc(2026, 9, 2, 0, 40)
        p, _ = self._pol(allowance=117, spent=0, exempt={"soccer"})
        engage = sum(3 for _ in range(20)
                     if p.allow("soccer_epl", "soccer", nuit, 2000, cost=3)[0])
        assert engage <= intraday_cap(117, nuit) * EXEMPT_SHARE
        assert engage < 37, "le tick de nuit reprend toute l'allocation"

    def test_sans_allocation_rien_ne_change(self):
        p, _ = self._pol(allowance=None, spent=10_000)
        assert p.allow("soccer_epl", "soccer", self.NIGHT, 2000, cost=3)[0]
        assert p.budget_left(self.NIGHT) is None

    def test_note_paid_persiste_le_cout(self):
        p, noted = self._pol(allowance=100)
        p.note_paid("soccer_epl", 3)
        p.note_paid("soccer_epl")            # ancien appel, sans coût : rien à persister
        assert noted == [3.0]

    def test_pacing_desactivable_par_env(self, monkeypatch):
        import core.scan_windows as sw
        monkeypatch.setattr(sw, "PACING_ENABLED", False)
        p = sw.SpendPolicy(lambda _k: None, lambda _k: None, allowance=1, spent_today=99)
        assert p.allowance is None
        assert p.allow("soccer_epl", "soccer", self.EVENING, 2000, cost=3)[0]


def test_fetch_odds_sert_les_ligues_les_plus_peuplees_d_abord(monkeypatch):
    """Quand le rythme ne laisse passer qu'une ligue, c'est celle qui
    rapporte le plus de matchs par crédit — pas la première du dict."""
    calls = []
    events = {"soccer_epl": 2, "baseball_kbo": 9}

    def fake_get(url, params=None, timeout=None):
        if url.rstrip("/").endswith("/sports"):
            return _Resp([])
        key = url.split("/sports/")[1].split("/")[0]
        if "/events" in url:
            return _Resp([{"id": str(i)} for i in range(events[key])])
        calls.append(key)
        return _Resp([])

    monkeypatch.setattr(odds_api.requests, "get", fake_get)
    monkeypatch.setattr(odds_api, "datetime", _FrozenDT)
    noted = []
    # Mardi 04:00 : les deux ligues sont de fond → plafond 1000 × 6/24 × 0,5 = 125.
    # À 123 engagés, il reste 2 crédits : KBO (h2h,totals = 2) passe, EPL (3) non.
    pol = SpendPolicy(lambda _k: None, lambda _k: None, allowance=1000, spent_today=123,
                      note_spent=noted.append)
    odds_api.fetch_odds(api_key="k", hours_ahead=24,
                        sport_keys={"soccer_epl": "soccer", "baseball_kbo": "baseball"},
                        spend_policy=pol)
    assert calls == ["baseball_kbo"]                 # 9 matchs pour 2 crédits, servie d'abord
    assert noted == [2.0]                            # coût réel (h2h,totals) persisté
    assert [k for k, _ in pol.skipped] == ["soccer_epl"]


def test_league_cost_suit_les_marches():
    assert odds_api.league_cost("soccer") == 3
    assert odds_api.league_cost("baseball") == 2
    assert odds_api.league_cost("mma") == 1
    assert odds_api.league_cost("inconnu") == 1


def test_pool_total_remaining_sonde_chaque_cle_une_fois(monkeypatch):
    odds_api.reset_pool()
    seen = []

    def fake_get(url, params=None, timeout=None):
        seen.append(params["apiKey"])
        left = {"a": "476", "b": "500", "c": "0"}[params["apiKey"]]
        r = _Resp([], remaining=left)
        r.headers["x-requests-used"] = str(500 - int(left))
        return r

    monkeypatch.setattr(odds_api.requests, "get", fake_get)
    monkeypatch.setattr(odds_api, "candidate_keys", lambda explicit=None: ["a", "b", "c"])
    assert odds_api.pool_total_remaining() == 976          # c : 0 crédit → morte, exclue
    assert odds_api.pool_total_remaining() == 976          # aucune nouvelle sonde
    assert seen == ["a", "b", "c"]
    t = odds_api.pool_totals()
    assert t["remaining"] == 976 and t["total"] == 1500 and abs(t["pct"] - 976 / 15) < 1e-9
    odds_api.reset_pool()
    assert odds_api.pool_totals() is None


def test_compteurs_meta_du_rythme(monkeypatch):
    """Cycle, dépense du jour et remise à zéro au changement de date — sur
    le double Supabase des tests moteur."""
    import run_engine as eng
    from tests.test_engine_circuit_breaker import FakeSB
    sb = FakeSB()
    now = datetime(2026, 9, 2, 12, tzinfo=timezone.utc)
    assert eng._oddsapi_spent_today(sb, now) == 0.0
    eng._oddsapi_note_spent(sb, now, 3)
    eng._oddsapi_note_spent(sb, now, 2)
    assert eng._oddsapi_spent_today(sb, now) == 5.0
    assert eng._oddsapi_spent_today(sb, now + timedelta(days=1)) == 0.0   # nouveau jour
    # Cycle : démarre à la première lecture (≈ 30 j restants), redémarre après 30 j.
    days = eng._oddsapi_cycle_days_left(sb, datetime.now(timezone.utc))
    assert 29.9 < days <= 30.0
    sb.store["oddsapi_cycle_start"]["value"] = (datetime.now(timezone.utc)
                                                - timedelta(days=31)).isoformat()
    days = eng._oddsapi_cycle_days_left(sb, datetime.now(timezone.utc))
    assert 29.9 < days <= 30.0


def test_build_spend_policy_porte_le_rythme(monkeypatch):
    import run_engine as eng
    from tests.test_engine_circuit_breaker import FakeSB
    sb = FakeSB()
    monkeypatch.setattr(eng, "_odds_pool_total_remaining", lambda: 2400)
    monkeypatch.setattr(eng, "_sports_with_imminent_signals", lambda _sb, _now: {"mma"})
    now = datetime.now(timezone.utc)
    pol = eng._build_spend_policy(sb, now)
    assert pol.allowance == pytest.approx(2400 / 30, rel=0.01)
    assert pol.exempt_sports == {"mma"}
    assert not hasattr(pol, "imminent_mode")   # le rang « golden T-2h » est parti avec le mode
    pol.note_paid("soccer_epl", 3)
    assert eng._oddsapi_spent_today(sb, now) == 3.0
    assert eng._build_spend_policy(None, now) is None
