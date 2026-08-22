"""
tests/test_save_preserving.py — `_save` ne détruit plus rien sous re-scan.

L'ancien delete-then-insert effaçait à chaque re-scan les colonnes de
clôture (`clv_pct_real`, `closing_*`) posées par run_closing_line/audit,
changeait l'`id` (donc `ai_learning_ledger.signal_id` n'était jamais
stable) et remettait `created_at` au dernier scan. Avec le mode REPRICE
(re-scan HORAIRE du même slate), chacun de ces trois défauts devenait
systémique. `_save` fait désormais select-then-update-or-insert, scopé
`status='active'`.
"""
import run_engine as eng


class _R:
    def __init__(self, data):
        self.data = data


class _Q:
    """Chaîne select/eq/order/limit/execute + update/insert sur une liste de
    lignes dict partagée (le `store` du FakeSB)."""

    def __init__(self, rows, calls):
        self.rows, self.calls = rows, calls
        self._filters, self._update, self._insert = {}, None, None

    def select(self, *_a, **_k):
        return self

    def eq(self, col, val):
        self._filters[col] = val
        return self

    def order(self, *_a, **_k):
        return self

    def limit(self, *_a, **_k):
        return self

    def update(self, payload):
        self._update = dict(payload)
        return self

    def insert(self, payload):
        self._insert = dict(payload)
        return self

    def _matching(self):
        return [r for r in self.rows
                if all(r.get(c) == v for c, v in self._filters.items())]

    def execute(self):
        if self._insert is not None:
            row = dict(self._insert)
            row.setdefault("id", max((r["id"] for r in self.rows), default=0) + 1)
            row.setdefault("created_at", "T-insert")
            self.rows.append(row)
            self.calls.append(("insert", row))
            return _R([row])
        if self._update is not None:
            hit = self._matching()
            for r in hit:
                r.update(self._update)
            self.calls.append(("update", self._filters.get("id"), self._update))
            return _R(hit)
        return _R(self._matching())


class FakeSB:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.calls = []

    def table(self, _name):
        return _Q(self.rows, self.calls)


def _payload(**over):
    base = {
        "match": "Arsenal vs Chelsea", "market": "AH 0.0",
        "match_id": "m1", "market_key": "h2h",
        "xbet_odd": 1.95, "edge_pct": 3.2, "kelly_pct": 0.5,
        "sharp_prob": 0.55, "status": "active", "scanned_at": "T-new",
    }
    base.update(over)
    return base


def test_rescan_updates_in_place_preserving_id_created_at_and_clv():
    sb = FakeSB([{
        "id": 7, "match_id": "m1", "market_key": "h2h", "status": "active",
        "created_at": "T-first", "edge_pct": 9.9, "scanned_at": "T-old",
        "clv_pct_real": 1.8, "closing_pinnacle_price": 1.84,
    }])
    assert eng._save(sb, _payload()) is True
    assert len(sb.rows) == 1
    row = sb.rows[0]
    assert row["id"] == 7                       # id stable pour le ledger
    assert row["created_at"] == "T-first"       # première émission conservée
    assert row["clv_pct_real"] == 1.8           # capture de clôture intacte
    assert row["closing_pinnacle_price"] == 1.84
    assert row["edge_pct"] == 3.2               # champs de scan rafraîchis
    assert row["scanned_at"] == "T-new"


def test_no_existing_row_inserts():
    sb = FakeSB()
    assert eng._save(sb, _payload()) is True
    assert len(sb.rows) == 1
    assert sb.calls[-1][0] == "insert"


def test_fallback_match_market_when_no_match_id():
    sb = FakeSB([{
        "id": 3, "match": "Arsenal vs Chelsea", "market": "AH 0.0",
        "match_id": "", "market_key": "", "status": "active",
        "created_at": "T-first", "clv_pct_real": 0.7,
    }])
    assert eng._save(sb, _payload(match_id="", market_key="")) is True
    assert len(sb.rows) == 1
    assert sb.rows[0]["id"] == 3
    assert sb.rows[0]["clv_pct_real"] == 0.7


def test_settled_row_is_never_resurrected():
    sb = FakeSB([{
        "id": 5, "match_id": "m1", "market_key": "h2h", "status": "settled",
        "created_at": "T-first", "outcome": "WIN",
    }])
    assert eng._save(sb, _payload()) is True
    # La settled reste intacte, une ligne active NEUVE est insérée à côté.
    assert len(sb.rows) == 2
    settled = [r for r in sb.rows if r["id"] == 5][0]
    assert settled["status"] == "settled" and settled["outcome"] == "WIN"
    fresh = [r for r in sb.rows if r["id"] != 5][0]
    assert fresh["status"] == "active"


def test_schema_mismatch_on_insert_strips_optional_cols():
    class _FailingFirstInsert(FakeSB):
        def __init__(self):
            super().__init__()
            self._failed = False

        def table(self, name):
            q = _Q(self.rows, self.calls)
            orig = q.execute
            outer = self

            def execute():
                if q._insert is not None and not outer._failed and \
                        "kelly_pct" in q._insert:
                    outer._failed = True
                    raise Exception('column "kelly_pct" does not exist')
                return orig()
            q.execute = execute
            return q

    sb = _FailingFirstInsert()
    assert eng._save(sb, _payload()) is True
    assert len(sb.rows) == 1
    assert "kelly_pct" not in sb.rows[0]        # colonne optionnelle retirée
    assert sb.rows[0]["edge_pct"] == 3.2        # le reste a survécu
