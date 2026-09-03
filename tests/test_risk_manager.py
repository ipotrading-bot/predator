"""
tests/test_risk_manager.py — core/risk_manager.py (Task 7): exposure cap
and rolling-drawdown circuit breaker.
"""
import pytest

import core.risk_manager as risk_manager
from core.constants import kelly_stake


class _Query:
    """Stub de lecture pour signals/ai_learning_ledger.

    Il FILTRE réellement depuis B4 (2026-08-27). Il ignorait auparavant tous
    les arguments, ce qui rendait invisible le défaut que B4 corrige : le
    disjoncteur tirait ses `window_n` dernières lignes TOUS STATUTS confondus
    puis filtrait en Python, si bien que sa fenêtre réelle tombait à UNE seule
    ligne décisive sur les vraies données. Un stub qui n'applique ni `in_()`
    ni `limit()` ne pouvait pas le montrer.
    """
    def __init__(self, rows):
        self._rows = list(rows)
        self._eq = {}
        self._in = {}
        self._limit = None
        self._desc = False
        self._order_col = None

    def select(self, *_a, **_k):
        return self

    def eq(self, col, value):
        self._eq[col] = value
        return self

    def in_(self, col, values):
        self._in[col] = list(values)
        return self

    def order(self, col, desc=False, **_k):
        self._order_col, self._desc = col, desc
        return self

    def limit(self, n, *_a, **_k):
        self._limit = n
        return self

    def execute(self):
        rows = [r for r in self._rows
                if all(r.get(c) == v for c, v in self._eq.items())
                and all(r.get(c) in vals for c, vals in self._in.items())]
        if self._order_col:
            rows.sort(key=lambda r: r.get(self._order_col) or "", reverse=self._desc)
        if self._limit is not None:
            rows = rows[:self._limit]
        return type("R", (), {"data": rows})()


class _MetaTable:
    """Real key-based upsert/select semantics — unlike _Query above, the
    circuit-breaker tests depend on resume_emission()'s upsert actually
    replacing the existing row for the same key, not just appending."""
    def __init__(self, rows_by_key: dict):
        self._rows_by_key = rows_by_key
        self._eq_key = None

    def select(self, *_a, **_k):
        return self

    def eq(self, col, value):
        if col == "key":
            self._eq_key = value
        return self

    def limit(self, *_a, **_k):
        return self

    def upsert(self, payload, **_k):
        self._rows_by_key[payload["key"]] = payload
        return self

    def execute(self):
        if self._eq_key is not None:
            row = self._rows_by_key.get(self._eq_key)
            return type("R", (), {"data": [row] if row else []})()
        return type("R", (), {"data": list(self._rows_by_key.values())})()


class _FakeSB:
    def __init__(self, signals=None, ledger=None, meta=None):
        self._signals = signals or []
        self._ledger = ledger or []
        self._meta_by_key = {m["key"]: m for m in (meta or [])}

    def table(self, name):
        if name == "signals":
            return _Query(self._signals)
        if name == "ai_learning_ledger":
            return _Query(self._ledger)
        if name == "meta":
            return _MetaTable(self._meta_by_key)
        raise AssertionError(f"unexpected table: {name}")

    @property
    def _meta(self):
        return list(self._meta_by_key.values())


def _active_signal(kelly_pct, is_shadow=False):
    # `is_shadow` est NOT NULL DEFAULT false en base (sql/migrate_v10_12) :
    # la fixture porte la même valeur par défaut que la ligne réelle.
    return {"kelly_pct": kelly_pct, "status": "active", "is_shadow": is_shadow}


def _ledger_row(outcome, kelly_pct=10.0, odds=2.0, created_at="2026-07-01T00:00:00",
                sport="soccer"):
    """`sport` est désormais RENSEIGNÉ. Il ne l'était pas, et le stub ignorant
    `eq()`, les tests du disjoncteur PAR SPORT passaient sans jamais vérifier
    qu'il était scopé à un sport : ils auraient été verts sur un disjoncteur
    global déguisé."""
    return {"outcome": outcome, "kelly_pct": kelly_pct, "odds": odds,
            "created_at": created_at, "sport": sport}


class TestGetCurrentExposure:
    def test_sums_kelly_pct_of_active_signals(self):
        sb = _FakeSB(signals=[_active_signal(5.0), _active_signal(3.0)])
        exposure = risk_manager.get_current_exposure(sb, bankroll=150)
        assert exposure == (5.0 + 3.0) / 100 * 150

    def test_missing_kelly_pct_contributes_zero(self):
        sb = _FakeSB(signals=[{"status": "active"}, _active_signal(5.0)])
        exposure = risk_manager.get_current_exposure(sb, bankroll=150)
        assert exposure == 5.0 / 100 * 150

    def test_read_failure_returns_zero(self):
        class _Boom:
            def table(self, _n):
                raise RuntimeError("down")
        assert risk_manager.get_current_exposure(_Boom(), bankroll=150) == 0.0

    def test_no_active_signals_is_zero_exposure(self):
        sb = _FakeSB(signals=[])
        assert risk_manager.get_current_exposure(sb, bankroll=150) == 0.0


class TestExposureHeadroom:
    def test_headroom_shrinks_as_exposure_grows(self):
        sb = _FakeSB(signals=[_active_signal(10.0)])
        headroom = risk_manager.get_exposure_headroom(sb, bankroll=150, max_pct=0.15)
        cap = 0.15 * 150
        exposure = 10.0 / 100 * 150
        assert headroom == cap - exposure

    def test_headroom_negative_once_cap_exceeded(self):
        sb = _FakeSB(signals=[_active_signal(50.0)])   # way over any 15% cap
        headroom = risk_manager.get_exposure_headroom(sb, bankroll=150, max_pct=0.15)
        assert headroom < 0

    def test_read_failure_fails_open_to_full_bankroll(self):
        class _Boom:
            def table(self, _n):
                raise RuntimeError("down")
        assert risk_manager.get_exposure_headroom(_Boom(), bankroll=150) == 150


class TestKellyStakeExposureAware:
    def test_zero_exposure_matches_baseline(self):
        stake_baseline = kelly_stake(2.0, 0.60, bankroll=150, sport="soccer")
        stake_explicit = kelly_stake(2.0, 0.60, bankroll=150, sport="soccer", current_exposure=0.0)
        assert stake_baseline == stake_explicit

    def test_exposure_at_full_bankroll_zeroes_stake(self):
        assert kelly_stake(2.0, 0.60, bankroll=150, sport="soccer", current_exposure=150) == 0

    def test_partial_exposure_reduces_but_does_not_necessarily_zero_stake(self):
        full = kelly_stake(2.0, 0.70, bankroll=150, sport="soccer", current_exposure=0)
        partial = kelly_stake(2.0, 0.70, bankroll=150, sport="soccer", current_exposure=100)
        assert partial <= full


class TestRollingDrawdown:
    def test_all_wins_zero_drawdown(self):
        rows = [_ledger_row("WIN", created_at=f"2026-07-0{i}T00:00:00") for i in range(1, 6)]
        assert risk_manager.rolling_drawdown(rows) == 0.0

    def test_losing_streak_produces_drawdown(self):
        rows = [_ledger_row("LOSS", kelly_pct=10, created_at=f"2026-07-0{i}T00:00:00") for i in range(1, 6)]
        dd = risk_manager.rolling_drawdown(rows)
        assert dd > 0

    def test_fewer_than_two_decisive_rows_returns_zero(self):
        assert risk_manager.rolling_drawdown([_ledger_row("WIN")]) == 0.0
        assert risk_manager.rolling_drawdown([]) == 0.0

    def test_recovery_after_drawdown_still_reports_the_peak_trough_gap(self):
        rows = (
            [_ledger_row("LOSS", kelly_pct=10, created_at="2026-07-01T00:00:00")] * 5
            + [_ledger_row("WIN", kelly_pct=10, created_at="2026-07-02T00:00:00")] * 3
        )
        dd = risk_manager.rolling_drawdown(rows)
        assert dd > 0   # the earlier drawdown doesn't get erased by later recovery


class TestCircuitBreaker:
    def test_trips_on_large_drawdown_and_persists_pause(self):
        rows = [_ledger_row("LOSS", kelly_pct=10, created_at=f"2026-07-01T00:{i:02d}:00") for i in range(20)]
        sb = _FakeSB(ledger=rows, meta=[])
        tripped = risk_manager.check_circuit_breaker(sb, window_n=20, limit_pct=0.25)
        assert tripped is True
        assert any(m.get("key") == "risk_circuit_breaker_paused" and m.get("value") == "true"
                  for m in sb._meta)

    def test_does_not_trip_on_healthy_record(self):
        rows = [_ledger_row("WIN", kelly_pct=10, created_at=f"2026-07-01T00:{i:02d}:00") for i in range(20)]
        sb = _FakeSB(ledger=rows, meta=[])
        assert risk_manager.check_circuit_breaker(sb, window_n=20, limit_pct=0.25) is False

    def test_already_paused_stays_tripped_regardless_of_current_window(self):
        # Even a currently-healthy window must not silently clear an
        # already-tripped breaker — only resume_emission() can.
        rows = [_ledger_row("WIN", kelly_pct=10, created_at=f"2026-07-01T00:{i:02d}:00") for i in range(20)]
        sb = _FakeSB(ledger=rows, meta=[{"key": "risk_circuit_breaker_paused", "value": "true"}])
        assert risk_manager.check_circuit_breaker(sb) is True

    def test_resume_emission_clears_the_pause(self):
        sb = _FakeSB(meta=[{"key": "risk_circuit_breaker_paused", "value": "true"}])
        risk_manager.resume_emission(sb)
        assert risk_manager.is_emission_paused(sb) is False


class TestSportCircuitBreaker:
    """A sport-scoped drawdown must be detectable/pausable independently of
    the global breaker — the whole point is to catch a bad streak the
    global check would dilute away behind other sports' good results."""

    def test_trips_on_large_drawdown_for_that_sport_only(self):
        rows = [_ledger_row("LOSS", kelly_pct=10, created_at=f"2026-07-01T00:{i:02d}:00") for i in range(20)]
        sb = _FakeSB(ledger=rows, meta=[])
        tripped = risk_manager.check_circuit_breaker_by_sport(sb, "soccer", window_n=20, limit_pct=0.25)
        assert tripped is True
        assert any(m.get("key") == "risk_circuit_breaker_paused_soccer" and m.get("value") == "true"
                  for m in sb._meta)

    def test_does_not_trip_on_healthy_record(self):
        rows = [_ledger_row("WIN", kelly_pct=10, created_at=f"2026-07-01T00:{i:02d}:00") for i in range(20)]
        sb = _FakeSB(ledger=rows, meta=[])
        assert risk_manager.check_circuit_breaker_by_sport(sb, "soccer", window_n=20, limit_pct=0.25) is False

    def test_one_sport_tripping_does_not_affect_another(self):
        rows = [_ledger_row("LOSS", kelly_pct=10, created_at=f"2026-07-01T00:{i:02d}:00") for i in range(20)]
        sb = _FakeSB(ledger=rows, meta=[])
        risk_manager.check_circuit_breaker_by_sport(sb, "soccer", window_n=20, limit_pct=0.25)
        assert risk_manager.is_sport_emission_paused(sb, "soccer") is True
        assert risk_manager.is_sport_emission_paused(sb, "basketball") is False

    def test_already_paused_stays_tripped_regardless_of_current_window(self):
        rows = [_ledger_row("WIN", kelly_pct=10, created_at=f"2026-07-01T00:{i:02d}:00") for i in range(20)]
        sb = _FakeSB(ledger=rows, meta=[{"key": "risk_circuit_breaker_paused_soccer", "value": "true"}])
        assert risk_manager.check_circuit_breaker_by_sport(sb, "soccer") is True

    def test_resume_sport_emission_clears_only_that_sport(self):
        sb = _FakeSB(meta=[
            {"key": "risk_circuit_breaker_paused_soccer", "value": "true"},
            {"key": "risk_circuit_breaker_paused_basketball", "value": "true"},
        ])
        risk_manager.resume_sport_emission(sb, "soccer")
        assert risk_manager.is_sport_emission_paused(sb, "soccer") is False
        assert risk_manager.is_sport_emission_paused(sb, "basketball") is True


class TestB4LaFenetreEstReelle:
    """B4 — le disjoncteur tirait ses `window_n` dernières lignes TOUS STATUTS
    confondus, puis filtrait en Python sur WIN/LOSS.

    Or le ledger est majoritairement fait d'`expired` : mesuré en base le
    2026-08-27, les 20 dernières lignes n'en contenaient qu'UNE SEULE de
    décisive. `rolling_drawdown` rendant 0.0 sous deux lignes, le disjoncteur
    ne pouvait PAS se déclencher. Il était inerte, en vert, depuis que les
    expirations dominent."""

    def _melange(self, n_expired, n_loss):
        """Des `expired` RÉCENTS et des pertes plus anciennes — l'ordre réel
        du ledger, où les expirations sont les dernières arrivées."""
        pertes = [_ledger_row("LOSS", kelly_pct=10,
                              created_at=f"2026-07-01T00:{i:02d}:00")
                  for i in range(n_loss)]
        expires = [_ledger_row("expired", kelly_pct=10,
                               created_at=f"2026-07-02T00:{i:02d}:00")
                   for i in range(n_expired)]
        return pertes + expires

    def test_les_expirations_ne_mangent_plus_la_fenetre(self):
        # 19 expirations récentes + 20 pertes : sans filtre SQL, la fenêtre de
        # 20 ne contiendrait qu'UNE perte et le drawdown vaudrait 0.
        sb = _FakeSB(ledger=self._melange(n_expired=19, n_loss=20), meta=[])
        assert risk_manager.check_circuit_breaker(sb, window_n=20,
                                                  limit_pct=0.25) is True

    def test_le_filtre_est_demande_a_la_base_pas_applique_apres_coup(self):
        vues = {}

        class _Espion(_Query):
            def in_(self, col, values):
                vues[col] = list(values)
                return super().in_(col, values)

        sb = _FakeSB(ledger=self._melange(19, 20), meta=[])
        sb.table = lambda nom: (_Espion(sb._ledger) if nom == "ai_learning_ledger"
                                else _MetaTable(sb._meta_by_key))
        risk_manager.check_circuit_breaker(sb, window_n=20, limit_pct=0.25)
        assert vues.get("outcome") == ["WIN", "LOSS"], \
            "le filtre doit partir dans la requête, sinon la fenêtre reste rognée"

    def test_le_disjoncteur_par_sport_filtre_aussi(self):
        rows = ([_ledger_row("expired", created_at=f"2026-07-02T00:{i:02d}:00")
                 for i in range(19)]
                + [_ledger_row("LOSS", kelly_pct=10,
                               created_at=f"2026-07-01T00:{i:02d}:00")
                   for i in range(20)])
        sb = _FakeSB(ledger=rows, meta=[])
        assert risk_manager.check_circuit_breaker_by_sport(
            sb, "soccer", window_n=20, limit_pct=0.25) is True


class TestB4UneCoteManquanteNestPasInventee:
    """La cote absente était remplacée par 2.0. À cote réelle 1,20, cela
    multipliait le gain par cinq et faisait remonter la courbe d'un pari qui
    n'avait presque rien rapporté. Un disjoncteur nourri de gains fictifs ne
    se déclenche jamais."""

    def test_un_gain_sans_cote_est_ecarte_pas_valorise_a_deux(self):
        rows = [_ledger_row("WIN", kelly_pct=10, odds=None,
                            created_at=f"2026-07-01T00:{i:02d}:00")
                for i in range(10)]
        rows += [_ledger_row("LOSS", kelly_pct=10,
                             created_at=f"2026-07-01T01:{i:02d}:00")
                 for i in range(10)]
        dd = risk_manager.rolling_drawdown(rows)
        # Les 10 gains sont écartés : la courbe ne fait que descendre,
        # 100 → 0, soit un drawdown de 100 %.
        # Avec l'ancien défaut à 2.0, les gains FICTIFS montaient d'abord la
        # courbe à 180 avant qu'elle ne retombe à 80 : 55,6 % seulement. Un
        # seuil de 25 % aurait été franchi dans les deux cas — c'est pourquoi
        # l'assertion doit porter sur la VALEUR, pas sur « ça se déclenche ».
        assert dd == pytest.approx(1.0, abs=1e-6), \
            "un gain sans cote a été valorisé au lieu d'être écarté"

    def test_une_perte_sans_cote_est_CONSERVEE(self):
        """Sens inverse, et c'est le point délicat : écarter les pertes ferait
        paraître le portefeuille plus sain qu'il n'est — exactement l'erreur
        qu'on corrige."""
        rows = [_ledger_row("WIN", kelly_pct=10, odds=3.0,
                            created_at="2026-07-01T00:00:00"),
                _ledger_row("LOSS", kelly_pct=10, odds=None,
                            created_at="2026-07-01T00:01:00")]
        assert risk_manager.rolling_drawdown(rows) > 0.0

    def test_une_cote_illisible_ne_leve_pas(self):
        rows = [_ledger_row("WIN", kelly_pct=10, odds="n/a",
                            created_at="2026-07-01T00:00:00"),
                _ledger_row("LOSS", kelly_pct=10,
                            created_at="2026-07-01T00:01:00"),
                _ledger_row("LOSS", kelly_pct=10,
                            created_at="2026-07-01T00:02:00")]
        assert risk_manager.rolling_drawdown(rows) > 0.0

    def test_une_cote_sous_le_pair_ne_credite_aucun_gain(self):
        # cote 1.00 = aucun gain possible ; la traiter comme valorisable
        # crediterait net_b(1.00) = 0, mais une cote <= 1.01 est une donnée
        # cassée, pas un pari à gain nul.
        rows = [_ledger_row("WIN", kelly_pct=10, odds=1.00,
                            created_at="2026-07-01T00:00:00"),
                _ledger_row("LOSS", kelly_pct=10,
                            created_at="2026-07-01T00:01:00"),
                _ledger_row("LOSS", kelly_pct=10,
                            created_at="2026-07-01T00:02:00")]
        assert risk_manager.rolling_drawdown(rows) > 0.0


class TestFantomesHorsExposition:
    def test_un_fantome_ne_compte_pas_dans_lexposition(self):
        """Un signal jamais recommandé n'engage aucun capital : le compter
        réduisait la marge (headroom) des paris réellement conseillés."""
        sb = _FakeSB(signals=[_active_signal(5.0), _active_signal(40.0, is_shadow=True)])
        assert risk_manager.get_current_exposure(sb, bankroll=100) == 5.0
