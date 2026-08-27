"""
tests/test_save_preserving.py — `_save` ne détruit plus rien sous re-scan.

L'ancien delete-then-insert effaçait à chaque re-scan les colonnes de
clôture (`clv_pct_real`, `closing_*`) posées par run_closing_line/audit,
changeait l'`id` (donc `ai_learning_ledger.signal_id` n'était jamais
stable) et remettait `created_at` au dernier scan. Avec le mode REPRICE
(re-scan HORAIRE du même slate), chacun de ces trois défauts devenait
systémique. `_save` faisait ensuite select-then-update-or-insert, scopé `status='active'`.

Depuis B2 (2026-08-27) il ne LIT plus avant d'écrire : il tente l'INSERT et
laisse la BASE arbitrer, via l'index unique partiel de
`sql/migrate_v10_7_signals_unique_active.sql`. Le SELECT préalable était une
course — deux runs qui se chevauchent lisaient tous les deux « aucune ligne »
puis inséraient tous les deux, sans qu'aucune erreur soit levée.

⚠️ `FakeSB` MODÉLISE DONC L'INDEX. Sans lui, ces tests éprouveraient une base
qui n'existe pas : l'INSERT passerait toujours et le chemin de collision — le
chemin nominal d'un re-scan — ne serait jamais exercé.
"""
import pytest

import run_engine as eng


class _R:
    def __init__(self, data):
        self.data = data


class UniqueViolation(RuntimeError):
    """Ce que Postgres renvoie sur l'index unique partiel de migrate_v10_7."""

    def __init__(self):
        super().__init__(
            '{"code":"23505","message":"duplicate key value violates unique '
            'constraint \"signals_active_match_market_uniq\""}')


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

    def _viole_lunicite(self, row: dict) -> bool:
        """Reproduit `signals_active_match_market_uniq` : un seul actif par
        (match_id, market_key), et seulement quand les deux sont renseignés —
        l'index exclut les clés vides, sinon deux matchs sans identifiant
        s'écraseraient l'un l'autre."""
        mid, mkey = row.get("match_id"), row.get("market_key")
        if row.get("status") != "active" or not mid or not mkey:
            return False
        return any(r.get("status") == "active" and r.get("match_id") == mid
                   and r.get("market_key") == mkey for r in self.rows)

    def execute(self):
        if self._insert is not None:
            if self._viole_lunicite(self._insert):
                self.calls.append(("insert-refused", dict(self._insert)))
                raise UniqueViolation()
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
        # Les SELECT sont TRACÉS eux aussi : c'est la lecture préalable qui
        # était la course, donc c'est elle que les tests doivent pouvoir
        # constater. Sans cette ligne, remettre le select-then-update ne
        # ferait tomber aucun test.
        self.calls.append(("select", dict(self._filters)))
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


def test_archive_before_purge_carries_clv_kelly_and_sharp_prob():
    """L'insert ledger manuel omettait clv_pct_real/kelly_pct/sharp_prob :
    tous les expirés arrivaient avec CLV NULL, affamant _clv_stats. Le
    passage par core.db.log_to_ledger doit les porter."""
    captured = []

    class _LedgerQ:
        def __init__(self, sink):
            self.sink = sink

        def insert(self, payload):
            self.sink.append(payload)
            return self

        def execute(self):
            return _R([])

    class _LedgerSB:
        def table(self, _name):
            return _LedgerQ(captured)

    sig = {
        "id": 42, "match": "Boca vs River", "sport": "soccer", "league": "Copa",
        "market_key": "h2h", "market": "AH 0.0", "selection_name": "Boca",
        "xbet_odd": 1.92, "pinnacle_price": 1.85, "edge_pct": 2.4,
        "kelly_pct": 0.61, "sharp_prob": 0.548, "clv_pct_real": 1.7,
        "closing_pinnacle_price": 1.81, "match_time": "2026-08-22T20:00:00+00:00",
        "scanned_at": "2026-08-22T10:00:00+00:00",
    }
    eng._archive_before_purge(_LedgerSB(), [sig])
    assert len(captured) == 1
    row = captured[0]
    assert row["outcome"] == "expired"
    assert row["clv_pct_real"] == 1.7
    assert row["kelly_pct"] == 0.61
    assert row["sharp_prob"] == 0.548
    assert row["closing_pinnacle_price"] == 1.81
    assert row["signal_id"] == 42
    assert row["time_to_match_minutes"] == 600


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


class TestB2LaBaseArbitre:
    """B2 — `_save` ne lit plus avant d'écrire. La décision appartient à
    l'index unique partiel, pas à un SELECT que le run d'à côté peut périmer
    entre la lecture et l'écriture."""

    def test_un_rescan_passe_par_linsert_refuse_puis_lupdate(self):
        sb = FakeSB([{
            "id": 7, "match_id": "m1", "market_key": "h2h", "status": "active",
            "created_at": "T-first", "edge_pct": 9.9,
        }])
        assert eng._save(sb, _payload()) is True
        types = [c[0] for c in sb.calls]
        assert "insert-refused" in types, \
            "l'INSERT doit être TENTÉ : c'est la base qui doit refuser"
        assert types[-1] == "update"
        assert len(sb.rows) == 1 and sb.rows[0]["id"] == 7

    def test_aucun_select_prealable_quand_la_cle_est_connue(self):
        """Le SELECT était la course. Avec un (match_id, market_key), il ne
        doit plus y en avoir : on tente, on encaisse le refus."""
        sb = FakeSB()
        eng._save(sb, _payload())
        assert [c[0] for c in sb.calls] == ["insert"]

    def test_une_ligne_reglee_entre_temps_libere_la_place(self):
        """Cas réel de course : l'INSERT est refusé, puis l'UPDATE ne trouve
        aucune ligne ACTIVE parce qu'elle vient d'être réglée. Abandonner
        perdrait le signal ; il faut retenter l'INSERT."""
        rows = [{"id": 7, "match_id": "m1", "market_key": "h2h",
                 "status": "active", "created_at": "T-first"}]
        sb = FakeSB(rows)
        vrai_table = sb.table
        etat = {"tours": 0}

        def _table(nom):
            q = vrai_table(nom)
            vrai_execute = q.execute

            def _execute():
                # Au PREMIER refus d'insert, la ligne bascule en 'settled' :
                # l'UPDATE qui suit ne trouvera rien.
                if q._insert is not None and etat["tours"] == 0 and rows:
                    etat["tours"] = 1
                    try:
                        return vrai_execute()
                    finally:
                        rows[0]["status"] = "settled"
                return vrai_execute()

            q.execute = _execute
            return q

        sb.table = _table
        assert eng._save(sb, _payload()) is True
        actives = [r for r in sb.rows if r["status"] == "active"]
        assert len(actives) == 1, "le signal ne doit pas être perdu"
        assert [r for r in sb.rows if r["status"] == "settled"], \
            "la ligne réglée reste intacte"

    def test_une_cle_absente_garde_lancien_chemin_avec_sa_course(self):
        """L'index exclut les clés vides : ces lignes ne sont pas protégées et
        `_save` le sait. Le documenter vaut mieux que de laisser croire que
        tout est couvert."""
        sb = FakeSB([{
            "id": 3, "match": "Arsenal vs Chelsea", "market": "AH 0.0",
            "match_id": "", "market_key": "", "status": "active",
            "created_at": "T-first",
        }])
        assert eng._save(sb, _payload(match_id="", market_key="")) is True
        assert [c[0] for c in sb.calls][-1] == "update"
        assert len(sb.rows) == 1

    def test_deux_matchs_sans_identifiant_ne_secrasent_pas(self):
        """C'est la raison d'être de la clause `match_id <> ''` dans l'index :
        sans elle, deux matchs distincts dépourvus d'id entreraient en conflit
        sur ('', 'h2h') et l'un écraserait l'autre."""
        sb = FakeSB()
        assert eng._save(sb, _payload(match_id="", market_key="",
                                      match="A vs B", market="AH 0.0")) is True
        assert eng._save(sb, _payload(match_id="", market_key="",
                                      match="C vs D", market="AH 0.0")) is True
        assert len(sb.rows) == 2
        assert {r["match"] for r in sb.rows} == {"A vs B", "C vs D"}


class TestB2ReconnaissanceDeLaViolation:
    """Une violation d'unicité non reconnue serait traitée comme une panne
    d'écriture : le signal serait perdu à chaque re-scan, en silence.

    La reconnaissance vit dans `core.db.is_unique_violation` — point UNIQUE,
    partagé avec `log_to_ledger`. Deux copies finiraient par diverger, et
    l'une des deux prendrait une collision normale pour une panne."""

    def test_la_reconnaissance_est_derivee_pas_recopiee(self):
        from core.db import is_unique_violation
        assert eng._is_unique_violation is is_unique_violation

    @pytest.mark.parametrize("err", [
        '{"code":"23505","message":"duplicate key value violates unique constraint"}',
        "duplicate key value violates unique constraint",
        'relation "signals_active_match_market_uniq" already exists',
        "UNIQUE constraint failed: signals.match_id",
    ])
    def test_les_formes_connues_sont_reconnues(self, err):
        assert eng._is_unique_violation(err) is True

    @pytest.mark.parametrize("err", [
        "connection timeout",
        'column "closing_source" does not exist',
        "FATAL: too many connections",
    ])
    def test_une_autre_panne_nest_pas_prise_pour_une_collision(self, err):
        assert eng._is_unique_violation(err) is False
