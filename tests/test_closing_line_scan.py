"""
tests/test_closing_line_scan.py — core/closing_line.py: closing prices taken
off the OddsAPI scan payload run_engine.py already downloaded.

This is the path that finally gives totals/spreads a real clv_pct_real. The
oracle in core/audit_engine.py never could: it returns a single number, the
ML/DNB favourite's price, so every non-h2h candidate was skipped before any
budget was spent (see tests/test_audit_engine.py::TestNonH2HMarkets — that
contract still holds, this module is what fills the gap).

Pinned here:
  - one price per market AND per side, for h2h / totals / spreads;
  - the closing price is built with the same maths as the entry price
    (consensus over pinnacle/circa/cris, Power-devigged DNB for soccer h2h),
    otherwise CLV would measure our own formula drift, not the market's;
  - a MOVED LINE is refused, never graded against a different bet;
  - clv_pct_real = (xbet_odd / close − 1) × 100, positive = we beat the close
    — same sign convention as the oracle path, since learning_layer consumes
    both without knowing which wrote a row;
  - nothing is priced outside the kickoff window, and nothing in-play.

No live HTTP/DB: the Supabase client and the write helper are stubbed.
"""
from datetime import datetime, timedelta, timezone

import pytest

import core.closing_line as cl


NOW = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)


def _event(**overrides) -> dict:
    """A scan event as core/odds_api.py._parse_event() builds it."""
    ev = {
        "id": "evt1",
        "match": "Flamengo vs Palmeiras",
        "home": "Flamengo",
        "away": "Palmeiras",
        "sport": "soccer",
        "league": "Brasileirão",
        "commence_time": (NOW + timedelta(minutes=45)).isoformat(),
        "odds_pinnacle": {"1": 2.00, "X": 3.40, "2": 4.00},
        "odds_1xbet":    {"1": 2.10, "X": 3.40, "2": 3.90},
        "totals_pinnacle": {"over": 1.90, "under": 1.95, "point": 2.5},
        "totals_1xbet":    {"over": 2.05, "under": 1.85, "point": 2.5},
        "spreads_pinnacle": {"home": 1.85, "away": 2.00, "point": -0.5,
                             "away_point": 0.5},
        "spreads_1xbet":    {"home": 1.95, "away": 1.92, "point": -0.5,
                             "away_point": 0.5},
    }
    ev.update(overrides)
    return ev


def _sig(**overrides) -> dict:
    sig = {
        "id": 7,
        "match_id": "evt1",
        "match": "Flamengo vs Palmeiras",
        "sport": "soccer",
        "market_key": "h2h",
        "selection_name": "Flamengo",
        "xbet_odd": 2.10,
        "status": "active",
    }
    sig.update(overrides)
    return sig


class _FakeSupabase:
    """Answers the one query capture_from_scan makes, records writes."""
    def __init__(self, rows):
        self._rows = rows
        self.written: list[dict] = []
        self.filters: list = []

    def table(self, _name):
        return self

    def select(self, *_a, **_k):
        return self

    def eq(self, *_a, **_k):
        return self

    def in_(self, _col, values):
        self.filters.append(list(values))
        return self

    def limit(self, *_a, **_k):
        return self

    def execute(self):
        return type("Res", (), {"data": self._rows})()


def _capture(monkeypatch, signals, events, **kwargs):
    """Run capture_from_scan with persistence stubbed.
    Returns (count, [payloads written])."""
    sb = _FakeSupabase(signals)
    written: list[dict] = []

    def fake_update(_sb, sid, fields, optional_cols=frozenset()):
        written.append({"id": sid, **fields})
        return True

    monkeypatch.setattr(cl, "update_signal_fields", fake_update)
    n = cl.capture_from_scan(sb, events, now=NOW, **kwargs)
    return n, written, sb


class TestTotals:
    def test_over_priced_on_its_own_side_and_line(self, monkeypatch):
        sig = _sig(market_key="totals_over", selection_name="Over 2.5",
                   xbet_odd=2.05)
        n, rows, _ = _capture(monkeypatch, [sig], [_event()])
        assert n == 1
        # Pinnacle-only consensus for the OVER side: 1.90, not the under's 1.95
        assert rows[0]["closing_pinnacle_price"] == pytest.approx(1.90)
        assert rows[0]["clv_pct_real"] == pytest.approx((2.05 / 1.90 - 1) * 100, abs=0.01)
        assert rows[0]["clv_pct_real"] > 0          # we beat the close
        assert rows[0]["closing_source"] == "oddsapi"

    def test_under_gets_the_under_price(self, monkeypatch):
        sig = _sig(market_key="totals_under", selection_name="Under 2.5",
                   xbet_odd=1.85)
        n, rows, _ = _capture(monkeypatch, [sig], [_event()])
        assert rows[0]["closing_pinnacle_price"] == pytest.approx(1.95)
        assert rows[0]["clv_pct_real"] < 0          # closed above our price

    def test_moved_line_is_refused_not_graded(self, monkeypatch):
        # We bet Over 2.5; the market closed on 2.75. That is a different
        # bet — comparing the two prices would invent a CLV number.
        ev = _event(totals_pinnacle={"over": 1.90, "under": 1.95, "point": 2.75})
        sig = _sig(market_key="totals_over", selection_name="Over 2.5")
        n, rows, _ = _capture(monkeypatch, [sig], [ev])
        assert n == 0
        assert rows == []

    def test_consensus_uses_circa_and_cris_like_the_entry_price(self, monkeypatch):
        ev = _event(totals_circa={"over": 1.92, "under": 1.93, "point": 2.5},
                    totals_cris={"over": 1.94, "under": 1.91, "point": 2.5})
        sig = _sig(market_key="totals_over", selection_name="Over 2.5")
        _, rows, _ = _capture(monkeypatch, [sig], [ev])
        # Weighted, so not the plain mean — but it must move off Pinnacle-alone
        # and stay inside the range of the three quotes.
        price = rows[0]["closing_pinnacle_price"]
        assert 1.90 < price < 1.94


class TestSpreads:
    def test_home_side(self, monkeypatch):
        sig = _sig(market_key="spreads_home", selection_name="Flamengo -0.5",
                   xbet_odd=1.95)
        n, rows, _ = _capture(monkeypatch, [sig], [_event()])
        assert rows[0]["closing_pinnacle_price"] == pytest.approx(1.85)
        assert rows[0]["clv_pct_real"] == pytest.approx((1.95 / 1.85 - 1) * 100, abs=0.01)

    def test_away_side_matches_on_the_mirrored_line(self, monkeypatch):
        # Signal carries "+0.5"; the event stores the HOME line (-0.5) plus
        # away_point. Matching against the home line would falsely reject.
        sig = _sig(market_key="spreads_away", selection_name="Palmeiras +0.5",
                   xbet_odd=1.92)
        n, rows, _ = _capture(monkeypatch, [sig], [_event()])
        assert n == 1
        assert rows[0]["closing_pinnacle_price"] == pytest.approx(2.00)

    def test_away_line_derived_when_away_point_absent(self, monkeypatch):
        ev = _event(spreads_pinnacle={"home": 1.85, "away": 2.00, "point": -0.5})
        sig = _sig(market_key="spreads_away", selection_name="Palmeiras +0.5",
                   xbet_odd=1.92)
        n, rows, _ = _capture(monkeypatch, [sig], [ev])
        assert n == 1

    def test_moved_handicap_is_refused(self, monkeypatch):
        ev = _event(spreads_pinnacle={"home": 1.85, "away": 2.00, "point": -1.5,
                                      "away_point": 1.5})
        sig = _sig(market_key="spreads_home", selection_name="Flamengo -0.5")
        n, rows, _ = _capture(monkeypatch, [sig], [ev])
        assert n == 0


class TestH2H:
    def test_soccer_closes_on_dnb_like_the_entry_price(self, monkeypatch):
        from core.math_engine import calc_dnb
        expected = calc_dnb(2.00, 4.00, 3.40)
        n, rows, _ = _capture(monkeypatch, [_sig()], [_event()])
        assert rows[0]["closing_pinnacle_price"] == pytest.approx(expected, abs=0.001)

    def test_non_soccer_uses_raw_moneyline(self, monkeypatch):
        ev = _event(sport="basketball", home="Lakers", away="Celtics",
                    odds_pinnacle={"1": 1.80, "X": 0.0, "2": 2.05})
        sig = _sig(sport="basketball", selection_name="Celtics", xbet_odd=2.20)
        n, rows, _ = _capture(monkeypatch, [sig], [ev])
        assert rows[0]["closing_pinnacle_price"] == pytest.approx(2.05)

    def test_flipped_favourite_is_still_priced(self, monkeypatch):
        # The oracle path writes clv_pct_real=None here — it only knows the
        # closing favourite. The scan payload has both sides, so backing the
        # underdog is priced normally.
        sig = _sig(selection_name="Palmeiras", xbet_odd=3.90)
        n, rows, _ = _capture(monkeypatch, [sig], [_event()])
        assert n == 1
        assert rows[0]["clv_pct_real"] is not None

    def test_unresolvable_selection_writes_nothing(self, monkeypatch):
        sig = _sig(selection_name="")
        n, rows, _ = _capture(monkeypatch, [sig], [_event()])
        assert n == 0
        assert rows == []


class TestWindow:
    def test_match_beyond_the_window_is_not_priced(self, monkeypatch):
        ev = _event(commence_time=(NOW + timedelta(minutes=400)).isoformat())
        n, rows, sb = _capture(monkeypatch, [_sig()], [ev])
        assert n == 0
        assert sb.filters == []          # not even a DB round-trip

    def test_started_match_is_never_priced(self, monkeypatch):
        # Past kickoff the feed carries in-play prices — not a closing line.
        ev = _event(commence_time=(NOW - timedelta(minutes=5)).isoformat())
        n, rows, _ = _capture(monkeypatch, [_sig()], [ev])
        assert n == 0

    def test_only_events_in_window_are_queried(self, monkeypatch):
        near = _event()
        far  = _event(id="evt2",
                      commence_time=(NOW + timedelta(hours=20)).isoformat())
        n, rows, sb = _capture(monkeypatch, [_sig()], [near, far])
        assert sb.filters == [["evt1"]]
        assert n == 1

    def test_signal_from_another_event_is_ignored(self, monkeypatch):
        # Defensive: the row set comes back from a filtered query, but a
        # mismatched match_id must never be priced off the wrong event.
        sig = _sig(match_id="evt-other")
        n, rows, _ = _capture(monkeypatch, [sig], [_event()])
        assert n == 0


class TestRobustness:
    def test_no_events_no_db_call(self, monkeypatch):
        n, rows, sb = _capture(monkeypatch, [_sig()], [])
        assert (n, rows, sb.filters) == (0, [], [])

    def test_missing_market_block_is_skipped_quietly(self, monkeypatch):
        ev = _event()
        ev.pop("totals_pinnacle")
        sig = _sig(market_key="totals_over", selection_name="Over 2.5")
        n, rows, _ = _capture(monkeypatch, [sig], [ev])
        assert n == 0

    def test_unknown_market_key_is_skipped(self, monkeypatch):
        sig = _sig(market_key="btts_yes", selection_name="Yes")
        n, rows, _ = _capture(monkeypatch, [sig], [_event()])
        assert n == 0

    def test_selection_point_parsing(self):
        assert cl._selection_point("Over 2.5") == 2.5
        assert cl._selection_point("Under 9") == 9.0
        assert cl._selection_point("Flamengo -1.5") == -1.5
        assert cl._selection_point("Palmeiras +0.5") == 0.5
        assert cl._selection_point("Flamengo") is None
        assert cl._selection_point("") is None
