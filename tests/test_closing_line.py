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

    def lt(self, *_a, **_k):
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

    def test_capture_stamps_closing_captured_at(self, monkeypatch):
        # Without this stamp a T-3h price is indistinguishable from a T-5min
        # one, and the CLV derived from it is uninterpretable.
        now = datetime.now(timezone.utc)
        sig = _sig(1, "Ajax vs Feyenoord", 2.00,
                   (now + timedelta(minutes=8)).isoformat())
        sb = _FakeSupabase([sig])
        monkeypatch.setattr(audit_engine, "get_pinnacle_price",
                            lambda match, sport, league: (1.80, "Ajax"))

        assert audit_engine.capture_closing_lines(sb) == 1
        stamped = sb.inserted[0]["closing_captured_at"]
        assert stamped
        taken = datetime.fromisoformat(stamped.replace("Z", "+00:00"))
        assert abs((taken - now).total_seconds()) < 60


class TestRefreshBeatsCronDrift:
    """The original bug: capture was one-shot and gated on
    closing_pinnacle_price IS NULL, inside a 5-min window, on a cron that
    actually fires every ~116 min. It captured 0 prices in 203 signals.
    Refreshing is what makes the result independent of when the cron lands."""

    def test_signal_with_existing_price_is_repriced_when_stale(self, monkeypatch):
        now = datetime.now(timezone.utc)
        sig = _sig(1, "Ajax vs Feyenoord", 2.00,
                   (now + timedelta(minutes=15)).isoformat())
        # Priced 90 min ago, far from kickoff — must be refined, not skipped.
        sig["closing_pinnacle_price"] = 1.95
        sig["closing_captured_at"] = (now - timedelta(minutes=90)).isoformat()
        sb = _FakeSupabase([sig])
        monkeypatch.setattr(audit_engine, "get_pinnacle_price",
                            lambda match, sport, league: (1.80, "Ajax"))

        assert audit_engine.capture_closing_lines(sb) == 1
        assert sb.inserted[0]["closing_pinnacle_price"] == 1.80

    def test_recent_capture_is_not_repriced(self, monkeypatch):
        # Bounds oracle spend: refreshing every run over a 4h window would
        # burn the budget on matches nowhere near kickoff.
        now = datetime.now(timezone.utc)
        sig = _sig(1, "Ajax vs Feyenoord", 2.00,
                   (now + timedelta(minutes=15)).isoformat())
        sig["closing_pinnacle_price"] = 1.95
        sig["closing_captured_at"] = (now - timedelta(minutes=2)).isoformat()
        sb = _FakeSupabase([sig])
        calls = []
        monkeypatch.setattr(audit_engine, "get_pinnacle_price",
                            lambda match, sport, league: calls.append(match) or (1.80, "Ajax"))

        assert audit_engine.capture_closing_lines(sb) == 0
        assert calls == []

    def test_row_without_stamp_is_repriced(self, monkeypatch):
        # Pre-migration rows carry a price but no stamp — refresh them so the
        # backlog converges instead of staying permanently uninterpretable.
        now = datetime.now(timezone.utc)
        sig = _sig(1, "Ajax vs Feyenoord", 2.00,
                   (now + timedelta(minutes=15)).isoformat())
        sig["closing_pinnacle_price"] = 1.95
        sb = _FakeSupabase([sig])
        monkeypatch.setattr(audit_engine, "get_pinnacle_price",
                            lambda match, sport, league: (1.80, "Ajax"))

        assert audit_engine.capture_closing_lines(sb) == 1

    def test_malformed_stamp_does_not_crash(self, monkeypatch):
        now = datetime.now(timezone.utc)
        sig = _sig(1, "Ajax vs Feyenoord", 2.00,
                   (now + timedelta(minutes=15)).isoformat())
        sig["closing_pinnacle_price"] = 1.95
        sig["closing_captured_at"] = "pas une date"
        sb = _FakeSupabase([sig])
        monkeypatch.setattr(audit_engine, "get_pinnacle_price",
                            lambda match, sport, league: (1.80, "Ajax"))

        assert audit_engine.capture_closing_lines(sb) == 1

    def test_window_covers_the_real_execution_gap(self):
        # Guard rail on the constant itself: the measured median gap between
        # actual executions is 116 min and the worst observed is 254. A window
        # narrower than that reintroduces the original silent-zero bug.
        assert audit_engine.CLOSING_LINE_WINDOW_MIN >= 240
        assert audit_engine.CLOSING_LINE_REFRESH_MIN < audit_engine.CLOSING_LINE_WINDOW_MIN


class TestMissedClosingLinesIsVisible:
    """A green run that captured nothing looked exactly like a green run with
    nothing to do — that is how this stayed broken for a month."""

    def test_counts_signals_that_passed_kickoff_unpriced(self):
        now = datetime.now(timezone.utc)
        missed = [_sig(i, f"H{i} vs A{i}", 2.0,
                       (now - timedelta(minutes=30)).isoformat()) for i in range(3)]
        assert audit_engine.count_missed_closing_lines(_FakeSupabase(missed)) == 3

    def test_zero_when_nothing_missed(self):
        assert audit_engine.count_missed_closing_lines(_FakeSupabase([])) == 0

    def test_db_error_degrades_to_zero(self):
        class _Boom:
            def table(self, _n):
                raise RuntimeError("db down")
        assert audit_engine.count_missed_closing_lines(_Boom()) == 0


class TestLeadTimeLabel:
    def test_minutes_then_hours(self):
        now = datetime.now(timezone.utc)
        lbl = audit_engine._lead_time_label
        assert lbl((now + timedelta(minutes=37)).isoformat(), now) == "37min"
        assert lbl((now + timedelta(minutes=134)).isoformat(), now) == "2h14"

    def test_missing_or_past_is_unknown(self):
        now = datetime.now(timezone.utc)
        lbl = audit_engine._lead_time_label
        assert lbl(None, now) == "?"
        assert lbl("pas une date", now) == "?"
        assert lbl((now - timedelta(minutes=5)).isoformat(), now) == "?"
