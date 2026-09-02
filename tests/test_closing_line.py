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

    def update(self, payload):
        # capture_closing_lines patches the row in place rather than
        # delete+insert — see core/db.update_signal_fields.
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
    """Depuis le 2026-09-02, capture_closing_lines lit l'EXCHANGE (Matchbook,
    Betfair optionnel) au lieu de l'oracle web-search supprimé avec
    Groq/Tavily. Le gros du contrat (même côté, DNB exigé au football, refus
    sinon) vit dans core/closing_line.capture_from_exchange, testé par
    tests/test_closing_line_exchange.py — ici on teste le CÂBLAGE."""

    def test_sans_candidat_aucun_prix_nest_charge(self, monkeypatch):
        appels = []
        import core.matchbook as matchbook
        monkeypatch.setattr(matchbook, "fetch_matchbook_prices",
                            lambda **k: appels.append(1) or {})
        assert audit_engine.capture_closing_lines(_FakeSupabase([])) == 0
        assert not appels, "aucun candidat : Matchbook ne doit pas être interrogé"

    def test_seuls_les_h2h_sont_candidats(self, monkeypatch):
        now = datetime.now(timezone.utc)
        sig = _sig(1, "Ajax vs Feyenoord", 2.00,
                   (now + timedelta(minutes=30)).isoformat())
        sig["market_key"] = "totals_over"
        appels = []
        import core.matchbook as matchbook
        monkeypatch.setattr(matchbook, "fetch_matchbook_prices",
                            lambda **k: appels.append(1) or {})
        assert audit_engine.capture_closing_lines(_FakeSupabase([sig])) == 0
        assert not appels

    def test_le_cablage_passe_les_pseudo_matchs_a_capture_from_exchange(self, monkeypatch):
        now = datetime.now(timezone.utc)
        sig = _sig(7, "Ajax vs Feyenoord", 2.00,
                   (now + timedelta(minutes=30)).isoformat())
        sig["match_id"] = "mid-7"
        vus = {}
        import core.matchbook as matchbook
        monkeypatch.setattr(matchbook, "fetch_matchbook_prices",
                            lambda **k: {"ajax_feyenoord": {"1": 1.9, "X": 3.4, "2": 4.2}})
        monkeypatch.delenv("BETFAIR_APP_KEY", raising=False)

        def _fake_capture(sb, matches, prices, now=None):
            vus["matches"] = matches
            vus["prices"] = prices
            return 1

        monkeypatch.setattr(audit_engine, "capture_from_exchange", _fake_capture)
        assert audit_engine.capture_closing_lines(_FakeSupabase([sig])) == 1
        (m,) = vus["matches"]
        assert m["id"] == "mid-7" and m["home"] == "Ajax" and m["away"] == "Feyenoord"
        assert vus["prices"]

    def test_aucun_prix_exchange_rend_zero_sans_planter(self, monkeypatch):
        now = datetime.now(timezone.utc)
        sig = _sig(1, "Ajax vs Feyenoord", 2.00,
                   (now + timedelta(minutes=30)).isoformat())
        sig["match_id"] = "mid-1"
        import core.matchbook as matchbook
        monkeypatch.setattr(matchbook, "fetch_matchbook_prices", lambda **k: {})
        monkeypatch.delenv("BETFAIR_APP_KEY", raising=False)
        assert audit_engine.capture_closing_lines(_FakeSupabase([sig])) == 0

    def test_no_candidates_returns_zero(self):
        assert audit_engine.capture_closing_lines(_FakeSupabase([])) == 0

    def test_loracle_web_nexiste_plus(self):
        """Gardien de la suppression : plus d'oracle LLM dans la capture."""
        assert not hasattr(audit_engine, "get_pinnacle_price")
        import inspect
        src = inspect.getsource(audit_engine)
        assert "oracle" not in src.lower() or "ancien" in src.lower() or "supprim" in src.lower()


class TestMatchesFromSignals:
    def test_derive_les_deux_noms_et_le_match_id(self):
        sig = _sig(1, "Ajax vs Feyenoord", 2.0, "2026-09-02T18:00:00+00:00")
        sig["match_id"] = "m1"
        (m,) = audit_engine._matches_from_signals([sig])
        assert (m["home"], m["away"], m["id"]) == ("Ajax", "Feyenoord", "m1")
        assert m["commence_time"] == "2026-09-02T18:00:00+00:00"

    def test_sans_vs_ou_sans_match_id_le_signal_est_ecarte(self):
        s1 = _sig(1, "Ajax - Feyenoord", 2.0, "2026-09-02T18:00:00+00:00")
        s1["match_id"] = "m1"
        s2 = _sig(2, "Ajax vs Feyenoord", 2.0, "2026-09-02T18:00:00+00:00")
        s2["match_id"] = ""
        assert audit_engine._matches_from_signals([s1, s2]) == []


class TestInvariantsHistoriques:
    def test_le_remplacement_de_ligne_nexiste_plus_dans_audit_engine(self):
        assert not hasattr(audit_engine, "replace_signal_row")


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
