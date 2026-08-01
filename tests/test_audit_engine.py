"""
tests/test_audit_engine.py — capture_closing_lines() market/side-awareness.

Before the 2026-07-11 fix, clv_pct_real was scan_pinnacle/close_pinnacle − 1
(Pinnacle self-drift — xbet_odd, the price the bettor actually got, never
entered the formula) and it was computed for EVERY candidate signal:
core/oracle.py's get_pinnacle_price() only ever quotes the closing ML/DNB
FAVORITE, so totals/spreads signals got a "closing line" from a different
market entirely, and h2h signals whose favorite flipped by kickoff got the
OTHER side's price. These tests pin the fixed contract:

  - clv_pct_real = (xbet_odd / closing_price_of_the_SAME_side − 1) × 100,
    positive = the bettor beat the close;
  - a side/market the oracle cannot resolve yields clv_pct_real=None —
    never a value derived from the wrong side or the wrong market;
  - non-h2h candidates are skipped BEFORE any oracle budget is spent.

No live HTTP/DB — oracle, candidate fetch, and persistence are all
monkeypatched at the core.audit_engine module level.
"""
import pytest

import core.audit_engine as audit_engine
from core.learning_layer import _clv_stats


def _sig(**overrides) -> dict:
    base = {
        "id": 42,
        "match": "America MG vs America RN",
        "sport": "soccer",
        "league": "Brasileirão",
        "market_key": "h2h",
        "selection_name": "America MG",
        "xbet_odd": 2.10,
        # Deliberately far from xbet_odd: the old formula divided THIS by the
        # closing price — any test asserting the new formula must still pass
        # if someone reintroduces the scan-price numerator.
        "pinnacle_price": 2.50,
        "status": "active",
    }
    base.update(overrides)
    return base


def _run_capture(monkeypatch, signals, oracle_result, budget=30):
    """Run capture_closing_lines with all I/O stubbed. Returns
    (captured_count, persisted_rows, oracle_call_count)."""
    persisted: list[dict] = []
    calls = {"oracle": 0}

    def fake_oracle(match, sport="soccer", league="", api_key=None, match_date=""):
        calls["oracle"] += 1
        return oracle_result

    monkeypatch.setattr(audit_engine, "fetch_closing_line_candidates", lambda sb: signals)
    monkeypatch.setattr(audit_engine, "get_pinnacle_price", fake_oracle)
    # capture_closing_lines writes with a plain UPDATE, never delete+insert:
    # it re-prices the same live signal every refresh, and replace_signal_row
    # would expose the row to loss (and a new id) on each one.
    monkeypatch.setattr(audit_engine, "update_signal_fields",
                        lambda sb, sid, fields, optional_cols=frozenset(): persisted.append(fields) or True)

    n = audit_engine.capture_closing_lines(sb=None, budget=budget)
    return n, persisted, calls["oracle"]


class TestH2HFavorite:
    def test_clv_is_xbet_vs_same_side_close_not_pinnacle_drift(self, monkeypatch):
        # Closing favorite == our selection → side resolved. xbet 2.10 vs
        # close 2.00 → +5.00%. The old formula would have said
        # 2.50/2.00−1 = +25% (scan-Pinnacle drift, nothing to do with the
        # bettor's price).
        n, rows, _ = _run_capture(monkeypatch, [_sig()], (2.00, "America MG"))
        assert n == 1
        assert rows[0]["closing_pinnacle_price"] == 2.00
        assert rows[0]["clv_pct_real"] == pytest.approx(5.0)

    def test_positive_means_bettor_beat_the_close(self, monkeypatch):
        # Line moved against us: close 2.20 > our 2.10 → negative CLV.
        n, rows, _ = _run_capture(monkeypatch, [_sig()], (2.20, "America MG"))
        assert rows[0]["clv_pct_real"] == pytest.approx((2.10 / 2.20 - 1) * 100, abs=0.01)
        assert rows[0]["clv_pct_real"] < 0

    def test_fuzzy_team_name_still_resolves(self, monkeypatch):
        # Oracle names differ cosmetically from ours ("FC" tag) — the same
        # normalization run_engine uses for cross-book matching applies,
        # as long as it points at exactly ONE side of the match.
        sig = _sig(match="Barcelona vs Real Madrid", selection_name="Barcelona")
        n, rows, _ = _run_capture(monkeypatch, [sig], (1.95, "FC Barcelona"))
        assert rows[0]["clv_pct_real"] == pytest.approx((2.10 / 1.95 - 1) * 100, abs=0.01)


class TestH2HUnresolvedSide:
    def test_flipped_favorite_writes_none_not_wrong_side_clv(self, monkeypatch):
        # By kickoff the OTHER team is the favorite: the fetched price is
        # for a side we didn't bet — clv_pct_real must be None, not a number
        # derived from the wrong side.
        n, rows, _ = _run_capture(monkeypatch, [_sig()], (1.85, "America RN"))
        assert len(rows) == 1
        assert rows[0]["clv_pct_real"] is None
        # Raw price still stored so the hourly job doesn't re-spend budget
        # on this signal (candidate filter is closing_pinnacle_price IS NULL).
        assert rows[0]["closing_pinnacle_price"] == 1.85

    def test_no_team_name_writes_none(self, monkeypatch):
        n, rows, _ = _run_capture(monkeypatch, [_sig()], (2.00, None))
        assert rows[0]["clv_pct_real"] is None

    def test_empty_selection_writes_none(self, monkeypatch):
        # strict_team_match() treats an empty name as a wildcard match —
        # an empty selection must NOT silently "resolve" against any team.
        n, rows, _ = _run_capture(monkeypatch, [_sig(selection_name="")], (2.00, "America MG"))
        assert rows[0]["clv_pct_real"] is None


class TestNonH2HMarkets:
    @pytest.mark.parametrize("market_key,selection", [
        ("totals_over",  "Over 2.5"),
        ("totals_under", "Under 2.5"),
        ("spreads_home", "America MG -1.5"),
        ("spreads_away", "America RN +1.5"),
    ])
    def test_never_calls_oracle_never_writes(self, monkeypatch, market_key, selection):
        # get_pinnacle_price only quotes the ML/DNB favorite — it cannot
        # return a totals or spreads price. Those signals must be skipped
        # BEFORE the oracle budget is spent, and nothing persisted (so no
        # misleading closing_pinnacle_price from another market).
        sig = _sig(market_key=market_key, selection_name=selection)
        n, rows, oracle_calls = _run_capture(monkeypatch, [sig], (2.00, "America MG"))
        assert n == 0
        assert rows == []
        assert oracle_calls == 0


class TestClvStatsConsumesSign:
    def test_positive_rate_counts_beating_the_close(self):
        # learning_layer._clv_stats consumes clv_pct_real with the same sign
        # convention the capture now writes: > 0 = bettor beat the close.
        rows = [{"clv_pct_real": 5.0}, {"clv_pct_real": -3.0}, {"clv_pct_real": None}]
        stats = _clv_stats(rows)
        assert stats["n"] == 2                      # None excluded, never defaulted
        assert stats["positive_rate"] == pytest.approx(0.5)
        assert stats["avg_clv"] == pytest.approx(1.0)
