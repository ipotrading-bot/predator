"""
tests/test_risk_manager.py — core/risk_manager.py (Task 7): exposure cap
and rolling-drawdown circuit breaker.
"""
import core.risk_manager as risk_manager
from core.constants import kelly_stake


class _Query:
    """Simple read-only stub for signals/ai_learning_ledger — returns
    whatever rows were preloaded, ignoring filter args (fine here since
    no test depends on eq()/order() actually filtering these two)."""
    def __init__(self, rows):
        self._rows = rows

    def select(self, *_a, **_k):
        return self

    def eq(self, *_a, **_k):
        return self

    def order(self, *_a, **_k):
        return self

    def limit(self, *_a, **_k):
        return self

    def execute(self):
        return type("R", (), {"data": self._rows})()


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


def _active_signal(kelly_pct):
    return {"kelly_pct": kelly_pct, "status": "active"}


def _ledger_row(outcome, kelly_pct=10.0, odds=2.0, created_at="2026-07-01T00:00:00"):
    return {"outcome": outcome, "kelly_pct": kelly_pct, "odds": odds, "created_at": created_at}


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
