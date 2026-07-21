"""
tests/test_audit_retry.py — a signal we merely FAILED TO LOOK UP must not be
stamped with a terminal status.

Regression cover for the 2026-07-21 incident (Actions run 29854918520): the
audit settled 16 signals, then compound-mini began returning "rate limit
minute" and the Tavily run budget ran out. The remaining 6 signals took the
Pass-2 branch and were written 'expired' — terminal, because fetch_pending()
only ever selects status='active'. Their matches had been over for up to 15h
with public final scores (Brewers 8-3 Mets, Rangers 3-10 White Sox, Storm
102-105 Lynx), so those WIN/LOSS outcomes were lost permanently and the
learning layer scored them as unknown.

The contract pinned here: 'closed'/'expired' may only be spent once the match
is older than EXPIRE_AFTER_H. Before that, a failed settlement returns
'skipped' and leaves the row 'active' for the next 6h run.

No live HTTP/DB — settlement, oracle and persistence are monkeypatched at the
core.audit_engine module level.
"""
from datetime import datetime, timedelta, timezone

import pytest

import core.audit_engine as audit_engine


NOW = datetime(2026, 7, 21, 18, 0, tzinfo=timezone.utc)


def _sig(hours_ago: float, **overrides) -> dict:
    base = {
        "id": 7553,
        "match": "Milwaukee Brewers vs New York Mets",
        "sport": "baseball",
        "league": "MLB",
        "market_key": "h2h",
        "selection_name": "Milwaukee Brewers",
        "xbet_odd": 1.50,
        "pinnacle_price": 1.47,
        "status": "active",
        "match_time": (NOW - timedelta(hours=hours_ago)).isoformat(),
    }
    base.update(overrides)
    return base


@pytest.fixture
def stubs(monkeypatch):
    """Settlement always fails, oracle always fails, search layer is alive.
    Records every write so a terminal stamp cannot slip through unnoticed."""
    state = {"updates": [], "ledger": []}
    monkeypatch.setattr(audit_engine, "settle_signal", lambda *a, **k: False)
    monkeypatch.setattr(audit_engine, "get_pinnacle_price", lambda *a, **k: (None, None))
    monkeypatch.setattr(audit_engine, "ai_available", lambda: True)
    monkeypatch.setattr(audit_engine, "gemini_quota_dead", lambda: False)
    monkeypatch.setattr(audit_engine, "search_exhausted", lambda: False)
    monkeypatch.setattr(audit_engine, "_update_signal",
                        lambda sb, sig, payload: state["updates"].append(payload) or True)
    monkeypatch.setattr(audit_engine, "log_to_ledger",
                        lambda sb, sig, clv, status: state["ledger"].append(status))
    return state


def _audit(sig):
    return audit_engine.audit_one(None, sig, [30], [30], NOW)


class TestFreshFailureIsRetried:
    def test_recent_match_stays_active(self, stubs):
        # 15h old — exactly the Brewers case that was wrongly expired.
        assert _audit(_sig(hours_ago=15)) == "skipped"

    def test_nothing_is_written_when_retrying(self, stubs):
        _audit(_sig(hours_ago=15))
        assert stubs["updates"] == []
        assert stubs["ledger"] == []

    def test_exhausted_search_budget_never_expires(self, stubs, monkeypatch):
        # Old enough to expire, but the search layer is out of credit — the
        # failure says nothing about the match, so it must not be terminal.
        monkeypatch.setattr(audit_engine, "search_exhausted", lambda: True)
        assert _audit(_sig(hours_ago=72)) == "skipped"
        assert stubs["ledger"] == []

    def test_missing_api_key_never_expires(self, stubs, monkeypatch):
        monkeypatch.setattr(audit_engine, "ai_available", lambda: False)
        assert _audit(_sig(hours_ago=72)) == "skipped"
        assert stubs["ledger"] == []


class TestStaleFailureStillCloses:
    def test_old_match_expires_terminally(self, stubs):
        assert _audit(_sig(hours_ago=audit_engine.EXPIRE_AFTER_H + 1)) == "expired"

    def test_old_match_reaches_the_ledger(self, stubs):
        _audit(_sig(hours_ago=audit_engine.EXPIRE_AFTER_H + 1))
        assert stubs["ledger"] == ["expired"]

    def test_real_closing_line_still_closes(self, stubs, monkeypatch):
        monkeypatch.setattr(audit_engine, "get_pinnacle_price", lambda *a, **k: (1.45, None))
        assert _audit(_sig(hours_ago=audit_engine.EXPIRE_AFTER_H + 1)) == "closed"

    def test_settlement_success_is_never_delayed(self, stubs, monkeypatch):
        # A signal young enough to retry must STILL settle immediately when
        # the score is actually found — the retry guard sits after Pass 1.
        monkeypatch.setattr(audit_engine, "settle_signal", lambda *a, **k: True)
        assert _audit(_sig(hours_ago=5)) == "settled"


class TestPastExpiryDating:
    def test_uses_match_time_first(self):
        assert audit_engine._past_expiry(_sig(hours_ago=100), NOW) is True
        assert audit_engine._past_expiry(_sig(hours_ago=1), NOW) is False

    def test_falls_back_to_scan_age_without_match_time(self):
        sig = _sig(hours_ago=1, match_time="")
        sig["scanned_at"] = (NOW - timedelta(hours=2)).isoformat()
        assert audit_engine._past_expiry(sig, NOW) is False
        sig["scanned_at"] = (NOW - timedelta(hours=200)).isoformat()
        assert audit_engine._past_expiry(sig, NOW) is True

    def test_naive_timestamp_is_treated_as_utc(self):
        # Supabase hands back "2026-07-20 23:41:00+00" for some columns and a
        # bare naive string for others; a naive value must not raise.
        sig = _sig(hours_ago=1, match_time="2026-07-20T23:41:00")
        assert audit_engine._past_expiry(sig, NOW) is False

    def test_undatable_signal_is_not_kept_active_forever(self):
        assert audit_engine._past_expiry(_sig(hours_ago=1, match_time="garbage"), NOW) is True
