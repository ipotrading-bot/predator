"""
tests/test_db.py — core/db.py credential resolution.

Regression guard for two incidents on 2026-07-07:
1. SUPABASE_SERVICE_KEY held a valid-but-wrong-privilege (anon) legacy JWT,
   which authenticated fine and then failed every write with RLS 42501 for
   ~17h before anyone noticed.
2. After that fix shipped, the same project's Vercel deployment kept
   failing — the value pasted there was a new-format Supabase key
   (`sb_secret_...` / `sb_publishable_...`, not a JWT at all), which the
   first fix's JWT-only decoder couldn't recognize and reported as
   role=None. get_db() must handle both key formats.
"""
import base64
import json

import pytest

from core.db import MissingCredentialsError, _key_role, get_db


def _jwt(role: str) -> str:
    header  = base64.urlsafe_b64encode(b'{"alg":"none"}').decode().rstrip("=")
    payload = base64.urlsafe_b64encode(json.dumps({"role": role}).encode()).decode().rstrip("=")
    return f"{header}.{payload}.sig"


class TestKeyRole:
    def test_decodes_legacy_jwt_service_role(self):
        assert _key_role(_jwt("service_role")) == "service_role"

    def test_decodes_legacy_jwt_anon(self):
        assert _key_role(_jwt("anon")) == "anon"

    def test_recognizes_new_format_secret_key(self):
        assert _key_role("sb_secret_abc123xyz") == "service_role"

    def test_recognizes_new_format_publishable_key(self):
        assert _key_role("sb_publishable_abc123xyz") == "anon"

    def test_garbage_token_returns_none(self):
        assert _key_role("not-a-jwt") is None

    def test_empty_string_returns_none(self):
        assert _key_role("") is None


class TestGetDb:
    def test_write_true_raises_on_missing_service_key(self, monkeypatch):
        monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
        monkeypatch.delenv("SUPABASE_SERVICE_KEY", raising=False)
        with pytest.raises(MissingCredentialsError):
            get_db(write=True)

    def test_write_true_raises_on_anon_key_in_service_slot(self, monkeypatch):
        # The 2026-07-07 failure mode #1: a syntactically valid legacy JWT
        # that authenticates fine but decodes to the wrong role.
        monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
        monkeypatch.setenv("SUPABASE_SERVICE_KEY", _jwt("anon"))
        with pytest.raises(MissingCredentialsError, match="anon"):
            get_db(write=True)

    def test_write_true_raises_on_publishable_key_in_service_slot(self, monkeypatch):
        # The 2026-07-07 failure mode #2: new-format key, wrong privilege.
        monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
        monkeypatch.setenv("SUPABASE_SERVICE_KEY", "sb_publishable_abc123")
        with pytest.raises(MissingCredentialsError, match="anon"):
            get_db(write=True)

    def test_write_true_succeeds_with_real_service_role_key(self, monkeypatch):
        monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
        monkeypatch.setenv("SUPABASE_SERVICE_KEY", _jwt("service_role"))
        sb = get_db(write=True)
        assert sb is not None

    def test_write_true_succeeds_with_new_format_secret_key(self, monkeypatch):
        monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
        monkeypatch.setenv("SUPABASE_SERVICE_KEY", "sb_secret_abc123xyz")
        sb = get_db(write=True)
        assert sb is not None

    def test_write_false_returns_none_without_raising_when_unconfigured(self, monkeypatch):
        monkeypatch.delenv("SUPABASE_URL", raising=False)
        monkeypatch.delenv("SUPABASE_KEY", raising=False)
        assert get_db(write=False) is None

    def test_write_false_never_requires_service_key(self, monkeypatch):
        monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
        monkeypatch.setenv("SUPABASE_KEY", "anon-key-stub")
        monkeypatch.delenv("SUPABASE_SERVICE_KEY", raising=False)
        assert get_db(write=False) is not None


class _StagedTable:
    """Rejects an insert if the payload contains any column in `missing`,
    exactly as Postgres/PostgREST does against a stale schema."""

    def __init__(self, missing, attempts):
        self._missing = missing
        self._attempts = attempts

    def delete(self):
        return self

    def eq(self, *_a, **_k):
        return self

    def insert(self, payload):
        self._attempts.append(dict(payload))
        bad = sorted(c for c in self._missing if c in payload)
        if bad:
            raise RuntimeError(
                f"could not find the '{bad[0]}' column of 'signals' in the schema cache")
        return self

    def execute(self):
        return type("Res", (), {"data": []})()


class _StagedSupabase:
    def __init__(self, missing):
        self._missing = missing
        self.attempts: list = []

    def table(self, _name):
        return _StagedTable(self._missing, self.attempts)


class TestOptionalColumnDegradationIsSurgical:
    """Une seule colonne manquante emportait toutes les autres colonnes
    optionnelles avec elle, en rapportant quand même un succès : l'appelant
    comptait une écriture qui n'avait jamais atterri. Seules les colonnes que
    Postgres NOMME peuvent être retirées.

    Ces cas portaient sur `replace_signal_row`, supprimée le 2026-08-27 (B1).
    Le contrat lui survit et appartient à `update_signal_fields`, désormais
    seul chemin d'écriture sur une ligne existante.
    """

    def test_falls_back_to_stripping_all_when_error_names_nothing(self):
        from core.db import update_signal_fields

        class _Opaque:
            def __init__(self):
                self.attempts = []

            def table(self, _n):
                outer = self

                class _T:
                    def update(self, payload):
                        outer.attempts.append(dict(payload))
                        self._payload = payload
                        return self

                    def eq(self, *_a, **_k):
                        return self

                    def execute(self):
                        # Erreur qui ne NOMME aucune colonne : impossible
                        # d'être chirurgical, le repli total est alors le
                        # dernier recours légitime.
                        if "closing_pinnacle_price" in self._payload:
                            raise RuntimeError("PGRST204 schema cache miss")
                        return type("R", (), {"data": []})()

                return _T()

        sb = _Opaque()
        ok = update_signal_fields(sb, 7, {"match": "A vs B",
                                          "closing_pinnacle_price": 1.8},
                                  optional_cols=frozenset({"closing_pinnacle_price"}))
        assert ok is True
        assert "closing_pinnacle_price" not in sb.attempts[-1]

    def test_returns_false_when_nothing_can_be_written(self):
        from core.db import update_signal_fields

        class _Dead:
            def table(self, _n):

                class _T:
                    def update(self, _payload):
                        return self

                    def eq(self, *_a, **_k):
                        return self

                    def execute(self):
                        raise RuntimeError("closing_captured_at missing and table gone")

                return _T()

        assert update_signal_fields(_Dead(), 7, {"match": "A vs B",
                                                 "closing_captured_at": "x"},
                                    optional_cols=frozenset({"closing_captured_at"})) is False

    def test_un_echec_ne_detruit_plus_rien(self):
        """La différence qui fonde B1. `replace_signal_row` SUPPRIMAIT avant
        d'insérer : un échec laissait le signal détruit, d'où son propre log
        CRITICAL « SIGNAL %s LOST after delete ». Un UPDATE raté ne touche
        rien et le tour suivant réessaie."""
        from core.db import update_signal_fields

        class _Dead:
            def table(self, _n):
                raise RuntimeError("db down")

        assert update_signal_fields(_Dead(), 7, {"clv_pct_real": 1.0}) is False


class TestLeDeleteInsertADisparu:
    """B1 — le garde porte sur l'ABSENCE. Tant qu'une fonction de
    remplacement de ligne existe quelque part, elle finit par être rappelée."""

    def test_core_db_nexpose_plus_de_remplacement_de_ligne(self):
        import core.db as db
        assert not hasattr(db, "replace_signal_row")

    def test_aucun_module_ne_reference_encore_le_remplacement_de_ligne(self):
        """Vérifié sur l'AST et non sur le texte : une docstring a le DROIT de
        nommer la fonction morte pour raconter pourquoi elle l'est — c'est même
        souhaitable. Ce qui est interdit, c'est de l'importer ou de l'appeler."""
        import ast
        import pathlib
        racine = pathlib.Path(__file__).resolve().parent.parent
        coupables = []
        for f in list(racine.glob("core/*.py")) + list(racine.glob("*.py")):
            arbre = ast.parse(f.read_text(encoding="utf-8"))
            for noeud in ast.walk(arbre):
                if isinstance(noeud, ast.Name) and noeud.id == "replace_signal_row":
                    coupables.append(f"{f.name}:{noeud.lineno}")
                elif isinstance(noeud, ast.Attribute) and noeud.attr == "replace_signal_row":
                    coupables.append(f"{f.name}:{noeud.lineno}")
                elif isinstance(noeud, (ast.Import, ast.ImportFrom)):
                    for a in noeud.names:
                        if a.name == "replace_signal_row":
                            coupables.append(f"{f.name}:{noeud.lineno} (import)")
                elif isinstance(noeud, ast.FunctionDef) and noeud.name == "replace_signal_row":
                    coupables.append(f"{f.name}:{noeud.lineno} (def)")
        assert coupables == [], coupables


    def test_ledger_keeps_kelly_when_only_the_stamp_is_missing(self):
        from core.db import log_to_ledger
        sb = _StagedSupabase(missing={"closing_captured_at"})
        sig = {
            "id": 1, "match": "A vs B", "sport": "soccer", "market_key": "h2h",
            "kelly_pct": 3.2, "sharp_prob": 0.58,
            "closing_captured_at": "2026-08-01T12:00:00+00:00",
        }
        log_to_ledger(sb, sig, clv=1.5, outcome="WIN")

        final = sb.attempts[-1]
        assert "closing_captured_at" not in final
        assert final["kelly_pct"] == 3.2
        assert final["sharp_prob"] == 0.58


class _UpdateTable:
    """Mimics PostgREST UPDATE against a stale schema: rejects a patch that
    names a column the table doesn't have."""

    def __init__(self, missing, attempts):
        self._missing = missing
        self._attempts = attempts

    def update(self, payload):
        self._attempts.append(dict(payload))
        bad = sorted(c for c in self._missing if c in payload)
        if bad:
            raise RuntimeError(
                f"could not find the '{bad[0]}' column of 'signals' in the schema cache")
        return self

    def eq(self, *_a, **_k):
        return self

    def execute(self):
        return type("Res", (), {"data": [{"id": 1}]})()


class _UpdateSupabase:
    def __init__(self, missing=frozenset()):
        self._missing = missing
        self.attempts: list = []

    def table(self, _name):
        return _UpdateTable(self._missing, self.attempts)


class TestUpdateSignalFields:
    """The closing-line job re-prices the same live signal every refresh.
    replace_signal_row() deletes before it inserts, so that path would expose
    the row to permanent loss — and a fresh id — on every single refresh."""

    def test_patches_only_the_given_fields(self):
        from core.db import update_signal_fields
        sb = _UpdateSupabase()
        ok = update_signal_fields(sb, 7, {"closing_pinnacle_price": 1.8,
                                          "clv_pct_real": 16.67})
        assert ok is True
        assert sb.attempts == [{"closing_pinnacle_price": 1.8, "clv_pct_real": 16.67}]

    def test_never_writes_id(self):
        from core.db import update_signal_fields
        sb = _UpdateSupabase()
        update_signal_fields(sb, 7, {"id": 99, "clv_pct_real": 1.0})
        assert "id" not in sb.attempts[-1]

    def test_missing_stamp_keeps_price_and_clv(self):
        from core.db import update_signal_fields
        sb = _UpdateSupabase(missing={"closing_captured_at"})
        ok = update_signal_fields(sb, 7, {
            "closing_pinnacle_price": 1.8,
            "clv_pct_real": 16.67,
            "closing_captured_at": "2026-08-01T12:00:00+00:00",
        }, optional_cols=frozenset(
            {"closing_pinnacle_price", "clv_pct_real", "closing_captured_at"}))

        assert ok is True
        final = sb.attempts[-1]
        assert "closing_captured_at" not in final
        assert final["closing_pinnacle_price"] == 1.8
        assert final["clv_pct_real"] == 16.67

    def test_failure_is_reported_not_swallowed(self):
        from core.db import update_signal_fields

        class _Dead:
            def table(self, _n):
                raise RuntimeError("db down")

        assert update_signal_fields(_Dead(), 7, {"clv_pct_real": 1.0}) is False

    def test_empty_patch_is_a_noop(self):
        from core.db import update_signal_fields
        sb = _UpdateSupabase()
        assert update_signal_fields(sb, 7, {}) is True
        assert sb.attempts == []


# ── B3 — le ledger n'enregistre qu'une fois par signal ───────────────────

class _LedgerViolation(RuntimeError):
    """Ce que Postgres renvoie sur `ledger_signal_id_uniq` (migrate_v10_8)."""

    def __init__(self):
        super().__init__(
            '{"code":"23505","message":"duplicate key value violates unique '
            'constraint \\"ledger_signal_id_uniq\\""}')


class _LedgerSB:
    """Faux Supabase qui MODÉLISE l'index unique partiel sur `signal_id`.

    Sans lui, ces tests éprouveraient une base qui n'existe pas : l'INSERT
    passerait toujours et le chemin d'idempotence ne serait jamais exercé.
    """

    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.calls = []

    def table(self, _name):
        outer = self

        class _T:
            def __init__(self):
                self._op = None
                self._payload = None
                self._filters = {}

            def insert(self, payload):
                self._op, self._payload = "insert", dict(payload)
                return self

            def update(self, payload):
                self._op, self._payload = "update", dict(payload)
                return self

            def select(self, *_a, **_k):
                self._op = "select"
                return self

            def eq(self, col, val):
                self._filters[col] = val
                return self

            def limit(self, *_a, **_k):
                return self

            def execute(self):
                if self._op == "insert":
                    sid = self._payload.get("signal_id")
                    if sid is not None and any(r.get("signal_id") == sid
                                               for r in outer.rows):
                        outer.calls.append(("insert-refused", sid))
                        raise _LedgerViolation()
                    row = dict(self._payload)
                    row.setdefault("id", len(outer.rows) + 1)
                    outer.rows.append(row)
                    outer.calls.append(("insert", sid))
                    return type("R", (), {"data": [row]})()
                if self._op == "update":
                    hit = [r for r in outer.rows
                           if all(r.get(c) == v for c, v in self._filters.items())]
                    for r in hit:
                        r.update(self._payload)
                    outer.calls.append(("update", dict(self._payload)))
                    return type("R", (), {"data": hit})()
                hit = [r for r in outer.rows
                       if all(r.get(c) == v for c, v in self._filters.items())]
                outer.calls.append(("select", dict(self._filters)))
                return type("R", (), {"data": hit})()

        return _T()


def _sig_ledger(**over):
    base = {"id": 7, "match": "A vs B", "sport": "soccer", "market_key": "h2h",
            "selection_name": "A", "xbet_odd": 1.90, "edge_pct": 3.0,
            "kelly_pct": 1.0, "sharp_prob": 0.55}
    base.update(over)
    return base


class TestLedgerIdempotent:
    """B3 — un doublon de ledger ne lève aucune erreur et ne se voit nulle
    part. Il gonfle simplement le `n` de `learning_layer`, qui resserre alors
    ses intervalles de Wilson SANS information nouvelle : la façon la plus
    discrète de se convaincre qu'on a prouvé quelque chose."""

    def test_un_premier_enregistrement_ecrit_la_ligne(self):
        from core.db import log_to_ledger
        sb = _LedgerSB()
        log_to_ledger(sb, _sig_ledger(), clv=1.0, outcome="WIN")
        assert len(sb.rows) == 1 and sb.rows[0]["signal_id"] == 7

    def test_un_second_enregistrement_ne_duplique_pas_ni_nalerte(self, caplog):
        """Deux exigences, et la seconde est celle qui manquait : sans la
        branche d'idempotence, l'insert échoue AUSSI sans dupliquer — mais il
        part alors en CRITICAL sur un fonctionnement parfaitement normal
        (audit rejoué, règlement retenté). Un CRITICAL qui crie pour rien finit
        par ne plus être lu."""
        import logging as _logging
        from core.db import log_to_ledger
        sb = _LedgerSB()
        log_to_ledger(sb, _sig_ledger(), clv=1.0, outcome="WIN")
        with caplog.at_level(_logging.INFO, logger="PREDATOR.db"):
            log_to_ledger(sb, _sig_ledger(), clv=1.0, outcome="WIN")
        assert len(sb.rows) == 1
        assert ("insert-refused", 7) in sb.calls
        assert not [r for r in caplog.records if r.levelno >= _logging.ERROR], \
            "une collision attendue ne doit pas produire d'erreur"
        assert any("déjà enregistré" in r.getMessage() for r in caplog.records)

    def test_un_resultat_reel_remplace_une_absence_de_resultat(self):
        """`_archive_before_purge` écrit `expired`, `settle_signal` un vrai
        WIN. Si l'expiration arrive la première, le résultat réel doit encore
        pouvoir la remplacer — sinon l'idempotence détruirait l'information
        qu'elle est censée protéger."""
        from core.db import log_to_ledger
        sb = _LedgerSB()
        log_to_ledger(sb, _sig_ledger(), clv=0.0, outcome="expired")
        log_to_ledger(sb, _sig_ledger(), clv=1.0, outcome="WIN")
        assert len(sb.rows) == 1
        assert sb.rows[0]["outcome"] == "WIN"

    def test_une_absence_de_resultat_ne_remplace_jamais_un_resultat_reel(self):
        """Le sens inverse est interdit : « le dernier gagne » remplacerait un
        WIN mesuré par un `expired` écrit ensuite par un autre chemin."""
        from core.db import log_to_ledger
        sb = _LedgerSB()
        log_to_ledger(sb, _sig_ledger(), clv=1.0, outcome="WIN")
        log_to_ledger(sb, _sig_ledger(), clv=0.0, outcome="expired")
        assert len(sb.rows) == 1
        assert sb.rows[0]["outcome"] == "WIN"

    def test_deux_resultats_decisifs_ne_secrasent_pas(self):
        from core.db import log_to_ledger
        sb = _LedgerSB()
        log_to_ledger(sb, _sig_ledger(), clv=1.0, outcome="WIN")
        log_to_ledger(sb, _sig_ledger(), clv=1.0, outcome="LOSS")
        assert sb.rows[0]["outcome"] == "WIN", \
            "le premier résultat décisif fait foi ; le second est un rejeu"

    def test_deux_signaux_distincts_sur_la_meme_affiche_coexistent(self):
        """Deux équipes se rencontrent deux fois par saison. C'est la raison
        pour laquelle la clé est `signal_id` et NON
        (match, market_type, selection) : ce triplet portait déjà des doublons
        légitimes en base (mesuré le 2026-08-27)."""
        from core.db import log_to_ledger
        sb = _LedgerSB()
        log_to_ledger(sb, _sig_ledger(id=7), clv=1.0, outcome="WIN")
        log_to_ledger(sb, _sig_ledger(id=8), clv=1.0, outcome="LOSS")
        assert len(sb.rows) == 2
        assert {r["outcome"] for r in sb.rows} == {"WIN", "LOSS"}

    def test_une_ligne_sans_signal_id_nest_pas_contrainte(self):
        # L'index est partiel : sans identifiant, pas de protection — et
        # surtout pas d'échec d'insertion.
        from core.db import log_to_ledger
        sb = _LedgerSB()
        log_to_ledger(sb, _sig_ledger(id=None), clv=1.0, outcome="WIN")
        log_to_ledger(sb, _sig_ledger(id=None), clv=1.0, outcome="WIN")
        assert len(sb.rows) == 2

    def test_une_relecture_impossible_ne_reecrit_rien(self):
        """Sur erreur de lecture après collision, ne rien faire est le
        comportement sûr : la ligne existe déjà."""
        from core.db import log_to_ledger
        sb = _LedgerSB([{"id": 1, "signal_id": 7, "outcome": "expired"}])
        vrai_table = sb.table

        def _table(nom):
            t = vrai_table(nom)
            vrai_execute = t.execute

            def _execute():
                if t._op == "select":
                    raise RuntimeError("lecture impossible")
                return vrai_execute()

            t.execute = _execute
            return t

        sb.table = _table
        log_to_ledger(sb, _sig_ledger(), clv=1.0, outcome="WIN")
        assert len(sb.rows) == 1
        assert sb.rows[0]["outcome"] == "expired"
