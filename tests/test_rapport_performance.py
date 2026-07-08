"""
tests/test_rapport_performance.py — run_rapport.py's _performance_block()
(Task 4): never show a bare win rate, always the Wilson 95% CI and the
tax-adjusted breakeven probability alongside it.
"""
import run_rapport


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def select(self, *_a, **_k):
        return self

    def order(self, *_a, **_k):
        return self

    def limit(self, *_a, **_k):
        return self

    def execute(self):
        return type("R", (), {"data": self._rows})()


class _FakeSB:
    def __init__(self, rows):
        self._rows = rows

    def table(self, _name):
        return _FakeQuery(self._rows)


def _row(outcome, odds=2.0):
    return {"outcome": outcome, "odds": odds}


class TestPerformanceBlock:
    def test_below_min_samples_returns_empty(self):
        sb = _FakeSB([_row("WIN") for _ in range(5)])
        assert run_rapport._performance_block(sb) == ""

    def test_includes_wilson_ci_and_breakeven(self):
        rows = [_row("WIN") for _ in range(15)] + [_row("LOSS") for _ in range(5)]
        sb = _FakeSB(rows)
        block = run_rapport._performance_block(sb)
        assert "IC 95%" in block
        assert "Seuil rentable" in block
        assert "75.0%" in block   # 15/20

    def test_query_failure_returns_empty_not_fatal(self):
        class _Boom:
            def table(self, _name):
                raise RuntimeError("supabase down")
        assert run_rapport._performance_block(_Boom()) == ""
