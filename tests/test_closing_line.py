"""
tests/test_closing_line.py — core/audit_engine.py real closing-line capture
(Task 3): capture_closing_lines() re-fetches the Pinnacle price ~5min
before kickoff and stores it as closing_pinnacle_price/clv_pct_real,
independent of and ahead of the match's real WIN/LOSS settlement.
"""
from datetime import datetime, timedelta, timezone

import core.audit_engine as audit_engine


class _FakeTable:
    def __init__(self, rows, inserted):
        self._rows = rows
        self._inserted = inserted

    def select(self, *_a, **_k):
        return self

    def eq(self, *_a, **_k):
        return self

    def is_(self, *_a, **_k):
        return self

    def gte(self, *_a, **_k):
        return self

    def lte(self, *_a, **_k):
        return self

    def limit(self, *_a, **_k):
        return self

    def delete(self):
        return self

    def insert(self, payload):
        self._inserted.append(payload)
        return self

    def execute(self):
        return type("Res", (), {"data": self._rows})()


class _FakeSupabase:
    def __init__(self, rows):
        self._rows = rows
        self.inserted: list = []

    def table(self, name):
        assert name == "signals"
        return _FakeTable(self._rows, self.inserted)


def _sig(id_, match, pinnacle_price, match_time, selection=None):
    home = match.split(" vs ")[0] if " vs " in match else match
    return {
        "id": id_,
        "match": match,
        "sport": "soccer",
        "league": "MLS",
        "market_key": "h2h",
        "selection_name": selection if selection is not None else home,
        "xbet_odd": 2.10,
        "pinnacle_price": pinnacle_price,
        "match_time": match_time,
        "status": "active",
    }


class TestCaptureClosingLines:
    def test_clv_real_computed_from_bet_price_vs_same_side_close(self, monkeypatch):
        # Real bettor CLV: the price WE got (xbet_odd) vs the closing price
        # of the SAME side — not pinnacle_price/close (Pinnacle self-drift),
        # which is why scan pinnacle_price (2.00) must not appear in the result.
        now = datetime.now(timezone.utc)
        sig = _sig(1, "Ajax vs Feyenoord", pinnacle_price=2.00,
                   match_time=(now + timedelta(minutes=3)).isoformat())
        sb = _FakeSupabase([sig])
        monkeypatch.setattr(audit_engine, "get_pinnacle_price",
                            lambda match, sport, league: (1.80, "Ajax"))

        n = audit_engine.capture_closing_lines(sb)

        assert n == 1
        assert len(sb.inserted) == 1
        payload = sb.inserted[0]
        assert payload["closing_pinnacle_price"] == 1.80
        assert payload["clv_pct_real"] == round((2.10 / 1.80 - 1) * 100, 2)

    def test_budget_limits_oracle_calls(self, monkeypatch):
        now = datetime.now(timezone.utc)
        sigs = [_sig(i, f"Home{i} vs Away{i}", 2.0, (now + timedelta(minutes=2)).isoformat())
                for i in range(5)]
        sb = _FakeSupabase(sigs)
        calls = []

        def fake_oracle(match, sport, league):
            calls.append(match)
            return 1.9, match.split(" vs ")[0]

        monkeypatch.setattr(audit_engine, "get_pinnacle_price", fake_oracle)

        n = audit_engine.capture_closing_lines(sb, budget=2)

        assert n == 2
        assert len(calls) == 2

    def test_oracle_failure_is_skipped_not_fatal(self, monkeypatch):
        now = datetime.now(timezone.utc)
        sig = _sig(1, "A vs B", 2.0, (now + timedelta(minutes=2)).isoformat())
        sb = _FakeSupabase([sig])

        def fake_oracle(*_a, **_k):
            raise RuntimeError("boom")

        monkeypatch.setattr(audit_engine, "get_pinnacle_price", fake_oracle)

        n = audit_engine.capture_closing_lines(sb)

        assert n == 0
        assert sb.inserted == []

    def test_no_candidates_returns_zero(self):
        sb = _FakeSupabase([])
        assert audit_engine.capture_closing_lines(sb) == 0
